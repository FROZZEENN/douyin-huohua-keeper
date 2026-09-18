"""发送引擎门面。

对外只暴露两个函数：

- :func:`send_to_contact` —— 给一个联系人发一条消息，返回结果
- :func:`describe_outcome` —— 把结果渲染成给人看的一句话

以及一个 :class:`Engine` 类，负责持有浏览器会话并在多次发送之间复用。

这一层**不做重试决策、不发通知、不写磁盘**。它只回答一个问题：
「这一次发送，成功了还是失败了，失败属于哪一类」。
重试由 scheduler 决定，告警由 notify 负责，落盘由 store 负责。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..common.errors import classify_exception
from ..config import BrowserSettings, SendSettings
from ..models import (
    Contact,
    FailureKind,
    Message,
    RunStatus,
    TargetOutcome,
)
from . import composer as compose_mod
from . import confirmer, navigator
from .auth import LoginCheck, probe_chat_page, verify_chat_page, verify_send_ready
from .browser import BrowserSession

LOGGER = logging.getLogger(__name__)


class EngineError(RuntimeError):
    """引擎层错误。"""


class AuthExpiredError(EngineError):
    """登录态失效，必须中止整个任务 —— 重试没有意义。"""


class RiskSuspectedError(EngineError):
    """疑似命中风控，必须降速并考虑中止。"""


@dataclass(frozen=True, slots=True)
class SendOutcome:
    """一次发送的结果。比 :class:`TargetOutcome` 多带一些诊断信息。"""

    contact_name: str
    status: RunStatus
    failure_kind: FailureKind | None = None
    sent_count: int = 0
    detail: str = ""
    observed_preview: str = ""
    elapsed_seconds: float = 0.0

    def to_target_outcome(self) -> TargetOutcome:
        return TargetOutcome(
            name=self.contact_name,
            status=self.status,
            sent=self.sent_count,
            failure_kind=self.failure_kind,
            detail=self.detail or None,
        )


class Engine:
    """持有浏览器会话，可在多次发送之间复用。

    复用而不是每次重开，是因为 Chromium 启动要几秒 —— 给三个联系人发消息
    没必要启动三次浏览器。
    """

    def __init__(self, browser_settings: BrowserSettings, send_settings: SendSettings) -> None:
        self.browser_settings = browser_settings
        self.send_settings = send_settings
        self._session: BrowserSession | None = None

    # --- 生命周期 -----------------------------------------------------------

    def start(self) -> None:
        if self._session is not None and self._session.alive:
            return
        self._session = BrowserSession(self.browser_settings)
        self._session.start()

    def stop(self) -> None:
        if self._session is not None:
            self._session.stop()
            self._session = None

    def __enter__(self) -> Engine:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    @property
    def session(self) -> BrowserSession:
        if self._session is None or not self._session.alive:
            raise EngineError("引擎尚未启动，请先调用 start()")
        return self._session

    # --- 登录态 -------------------------------------------------------------

    def load_state(self, state_path: Any) -> None:
        """把登录态塞进浏览器上下文。

        在 :meth:`start` 之后、任何页面操作之前调用。

        **cookie 与 localStorage 一起恢复。** 以前只回填 cookie，但
        storage_state 里还有抖音前端 SDK 自己写的 localStorage
        （``security-sdk/...``、``xmst``、``web_secsdk_runtime_cache`` 等）。
        缺了这些，SDK 可能会重新做一次安全校验 —— 表现为「cookie 明明在，
        却仍被当成未登录」。把它们一起灌回去，让会话更像一个真实浏览器，
        没有任何副作用（本来就是从同一个浏览器存出来的）。
        """
        session = self.session
        payload = _load_state_payload(state_path)

        def apply(context) -> None:
            cookies = _cookies_from_payload(payload)
            if cookies:
                context.context.add_cookies(cookies)
            script = _local_storage_seed_script(payload)
            if script:
                # add_init_script 在**页面自身脚本执行之前**运行，
                # 保证抖音前端第一次读 localStorage 时这些值就已经在了。
                context.context.add_init_script(script)

        session.call(apply)

    def check_login(self) -> LoginCheck:
        """打开会话页确认登录态是否有效。

        这是**有副作用**的检查（会导航页面），所以每次任务开始时调一次就好，
        不要塞进收件人循环里。
        """
        session = self.session
        return session.call(lambda ctx: probe_chat_page(ctx.page))

    # --- 发送 ---------------------------------------------------------------

    def send_to_contact(
        self,
        contact: Contact,
        message: Message,
        *,
        dry_run: bool = False,
        allow_search: bool = False,
    ) -> SendOutcome:
        """给一个联系人发送一条消息。

        流程：
        1. 检查输入框就绪（快速判断登录态是否还在）
        2. 定位并打开目标会话（打开后会校验头部名字，防发错人）
        3. 输入消息
        4. 触发发送
        5. 观察终态确认

        **契约：任何一步失败都返回带 ``failure_kind`` 的 SendOutcome，不抛异常** ——
        让上层能针对不同失败类型做不同决策。

        ⚠️ 这个契约以前是**破的**：``session.call`` 在浏览器线程挂掉、操作超时
        或空闲回收刚好把它关掉时会抛 ``BrowserError``，它会直接穿透出这个函数，
        于是上层拿不到逐人的失败分类，整个任务只会被判一次笼统的失败。这里包一层。
        """
        try:
            return self._send_to_contact(
                contact, message, dry_run=dry_run, allow_search=allow_search
            )
        except Exception as exc:  # 兜住一切，见上面「契约」那段说明
            LOGGER.exception("发送给 %s 时出现未预期异常", contact.name)
            return SendOutcome(
                contact_name=contact.name,
                status=RunStatus.FAILED,
                failure_kind=classify_exception(exc),
                detail=f"{type(exc).__name__}: {exc}",
            )

    def _send_to_contact(
        self,
        contact: Contact,
        message: Message,
        *,
        dry_run: bool = False,
        allow_search: bool = False,
    ) -> SendOutcome:
        """真正的发送实现。异常会穿透到 :meth:`send_to_contact` 统一兜住。"""
        import time as _time

        started = _time.monotonic()
        session = self.session

        # --- 1. 页面就绪检查 ---
        #
        # ⚠️ 这一步**不能用「输入框在不在」来判断**。
        #
        # 消息输入框是跟着会话详情一起出现的：刚进 /chat 时页面上只有
        # 搜索框和一列会话行，**一个 contenteditable 都没有**，
        # 点开某个会话后才出现。旧实现这里调的是 verify_send_ready
        # （查输入框），于是每个收件人都必然 COMPOSER_MISSING，
        # 还会被归类成 AUTH 失败、直接中止整个任务 —— 实测踩过。
        #
        # 现在改用会话页的**结构特征**判断（那些在未打开会话时就存在）。
        ready = session.call(lambda ctx: verify_chat_page(ctx.page))
        if not ready.ok:
            kind = FailureKind.AUTH if ready.reason == "AUTH_EXPIRED" else FailureKind.TRANSIENT
            return SendOutcome(
                contact_name=contact.name,
                status=RunStatus.FAILED,
                failure_kind=kind,
                detail=ready.detail,
                elapsed_seconds=_time.monotonic() - started,
            )

        # --- 2. 定位并打开会话 ---
        try:
            entry = session.call(
                lambda ctx: navigator.locate_conversation(
                    ctx.page, contact.name, allow_search=allow_search
                )
            )
        except navigator.ContactNotFoundError as exc:
            return SendOutcome(
                contact_name=contact.name,
                status=RunStatus.FAILED,
                failure_kind=FailureKind.PERMANENT,
                detail=str(exc),
                elapsed_seconds=_time.monotonic() - started,
            )
        except navigator.ConversationOpenError as exc:
            return SendOutcome(
                contact_name=contact.name,
                status=RunStatus.FAILED,
                failure_kind=FailureKind.TRANSIENT,
                detail=str(exc),
                elapsed_seconds=_time.monotonic() - started,
            )

        # --- 3. 输入框就绪检查 ---
        #
        # 会话已经打开了，输入框这时**才**应该存在。
        # 到这里还是没有，才真的说明掉了登录 / 页面结构变了。
        ready = session.call(lambda ctx: verify_send_ready(ctx.page))
        if not ready.ok:
            kind = FailureKind.AUTH if ready.reason.startswith("COMPOSER") else FailureKind.TRANSIENT
            return SendOutcome(
                contact_name=contact.name,
                status=RunStatus.FAILED,
                failure_kind=kind,
                detail=ready.detail,
                elapsed_seconds=_time.monotonic() - started,
            )

        # --- 3.5 抓「发送前」的会话基线 ---
        #
        # ⚠️ 必须在**输入/发送之前**抓。确认阶段要靠它回答
        #    「页面到底有没有变化」—— 只比对终态是旧实现的致命缺陷：
        #    消息恒为「1」时，列表预览本来就是「1」（昨天发的），
        #    于是什么都没发也会被判成功（实测事故）。
        #
        # 抓不到不致命：没有基线时确认会退化成更保守的判定
        # （报 UNCERTAIN 而不是 SUCCESS），不会误报成功。
        baseline = None
        try:
            expected_hint = _expected_text_for(message)
            baseline = session.call(
                lambda ctx: confirmer.snapshot_conversation(
                    ctx.page, entry.name, expected_text=expected_hint
                )
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("抓取会话基线失败（不影响发送，只会让确认更保守）：%s", exc)

        # --- 4. 输入消息 ---
        try:
            composed = session.call(lambda ctx: compose_mod.compose(ctx.page, message, dry_run=dry_run))
        except compose_mod.ComposeError as exc:
            return SendOutcome(
                contact_name=contact.name,
                status=RunStatus.FAILED,
                failure_kind=_classify_compose_error(exc),
                detail=str(exc),
                elapsed_seconds=_time.monotonic() - started,
            )

        # 演练模式到此为止
        if dry_run:
            return SendOutcome(
                contact_name=contact.name,
                status=RunStatus.SUCCESS,
                sent_count=1,
                detail=f"[演练] 已走到「输入完成」这一步，内容：{composed.text[:50]}",
                elapsed_seconds=_time.monotonic() - started,
            )

        # --- 5. 触发发送 ---
        try:
            session.call(lambda ctx: compose_mod.send(ctx.page))
        except compose_mod.ComposeError as exc:
            # 输入成功但发送失败，把输入框清掉，避免残留内容影响下一次
            session.call(lambda ctx: compose_mod.clear_input(ctx.page))
            return SendOutcome(
                contact_name=contact.name,
                status=RunStatus.FAILED,
                failure_kind=FailureKind.TRANSIENT,
                detail=str(exc),
                elapsed_seconds=_time.monotonic() - started,
            )

        # --- 5. 确认终态 ---
        expected_text = composed.text
        if composed.kind == "sticker":
            expected_text = f"[{composed.text}]"
        elif composed.kind == "image":
            expected_text = "[图片]"

        outcome = session.call(
            lambda ctx: confirmer.confirm_sent(
                ctx.page,
                contact_name=entry.name,
                expected_text=expected_text,
                timeout_ms=self.send_settings.confirm_timeout_ms,
                baseline=baseline,
            )
        )

        return SendOutcome(
            contact_name=contact.name,
            status=outcome.status,
            failure_kind=outcome.failure_kind,
            sent_count=1 if outcome.status is RunStatus.SUCCESS else 0,
            detail=outcome.detail,
            observed_preview=outcome.observed_preview,
            elapsed_seconds=_time.monotonic() - started,
        )


def describe_outcome(outcome: SendOutcome) -> str:
    """把结果渲染成给人看的一句话。工作台和日志都用它，保证措辞一致。"""
    icon = {
        RunStatus.SUCCESS: "✅",
        RunStatus.FAILED: "❌",
        RunStatus.SKIPPED: "⏭",
        RunStatus.UNCERTAIN: "❓",
    }.get(outcome.status, "•")

    text = f"{icon} {outcome.contact_name}：{outcome.detail or outcome.status.value}"

    if outcome.failure_kind == FailureKind.AUTH:
        text += "（登录态失效，需要重新扫码）"
    elif outcome.failure_kind == FailureKind.RISK:
        text += "（疑似风控，建议暂停一天）"
    elif outcome.failure_kind == FailureKind.PERMANENT:
        text += "（重试不会改善）"
    elif outcome.failure_kind == FailureKind.TRANSIENT:
        text += "（可重试）"

    return text


def _expected_text_for(message: Message) -> str:
    """这条消息发出去后「在页面上应该长成什么样」。

    确认阶段要拿它去比对（会话列表预览 / 消息区行）。
    口径必须和 :meth:`Engine._send_to_contact` 里确认时用的一致，
    否则「消息区行数」这条判据会因为两边字符串不同而永远读不到。
    """
    if message.kind == "text":
        return (message.content or "").strip()
    if message.kind == "sticker":
        return f"[{message.sticker}]"
    if message.kind == "image":
        return "[图片]"
    return ""


def _classify_compose_error(exc: compose_mod.ComposeError) -> FailureKind:
    """把输入阶段的错误归类。

    「找不到输入框」通常是登录态失效；「找不到表情」是配置问题；
    其余按临时性错误处理（页面加载不完整等）。
    """
    if isinstance(exc, compose_mod.StickerNotFoundError):
        return FailureKind.CONFIG

    text = str(exc)
    if "输入框" in text or "登录" in text:
        return FailureKind.AUTH
    if "不存在" in text and "文件" in text:
        return FailureKind.CONFIG

    return FailureKind.TRANSIENT


def _load_state_payload(state_path: Any) -> dict[str, Any]:
    """读取登录态文件，返回原始 ``storage_state`` 字典。"""
    import json
    from pathlib import Path

    path = Path(state_path)
    if not path.is_file():
        raise EngineError(f"登录态文件不存在：{path}\n请先在浏览器里扫码登录。")

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EngineError(
            f"登录态文件无法解析：{path}\n"
            f"原因：{type(exc).__name__}: {exc}\n"
            "可以删除该文件后重新扫码，或检查是不是被其他程序改坏了。"
        ) from exc

    if not isinstance(payload, dict):
        raise EngineError(f"登录态文件结构不对（顶层不是对象）：{path}")
    return payload


def _cookies_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """从 storage_state 字典里取出 cookie 列表，并清理成 Playwright 能接受的形状。"""
    cookies = payload.get("cookies")
    if not isinstance(cookies, list):
        raise EngineError("登录态里没有 cookies 字段，可能保存不完整")

    cleaned: list[dict[str, Any]] = []
    for cookie in cookies:
        if not isinstance(cookie, dict) or not cookie.get("name"):
            continue
        item = {
            "name": cookie["name"],
            "value": cookie.get("value", ""),
            "domain": cookie.get("domain", ".douyin.com"),
            "path": cookie.get("path", "/"),
        }
        if isinstance(cookie.get("expires"), (int, float)) and cookie["expires"] > 0:
            item["expires"] = cookie["expires"]
        if cookie.get("httpOnly"):
            item["httpOnly"] = True
        if cookie.get("secure"):
            item["secure"] = True
        if cookie.get("sameSite") in {"Strict", "Lax", "None"}:
            item["sameSite"] = cookie["sameSite"]
        cleaned.append(item)

    return cleaned


def _cookies_from_state(state_path: Any) -> list[dict[str, Any]]:
    """从 storage_state 文件里取出 cookie 列表（保留给旧调用点用）。"""
    return _cookies_from_payload(_load_state_payload(state_path))


def _local_storage_seed_script(payload: dict[str, Any]) -> str:
    """生成一段「把 localStorage 灌回去」的初始化脚本；没有可恢复项时返回空串。

    为什么要它：``storage_state`` 里的 ``origins[].localStorage`` 是抖音前端
    SDK 自己写进去的（安全 SDK 证书、``xmst`` 等）。只恢复 cookie、丢掉这些，
    SDK 可能会重新走一遍安全校验，进而把会话判为不可信 —— 也就是
    「cookie 都在，却仍然显示登录页」。
    """
    import json

    origins = payload.get("origins")
    if not isinstance(origins, list):
        return ""

    table: dict[str, dict[str, str]] = {}
    for entry in origins:
        if not isinstance(entry, dict):
            continue
        origin = entry.get("origin")
        items = entry.get("localStorage")
        if not isinstance(origin, str) or not origin or not isinstance(items, list):
            continue
        pairs: dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if isinstance(name, str) and name:
                pairs[name] = str(item.get("value") or "")
        if pairs:
            table[origin] = pairs

    if not table:
        return ""

    # 用 json.dumps 安全地把值嵌进 JS（避免引号 / 换行把脚本写坏）
    data = json.dumps(table, ensure_ascii=False)
    return (
        "(() => {\n"
        f"  const TABLE = {data};\n"
        "  try {\n"
        "    const items = TABLE[window.location.origin];\n"
        "    if (items) { for (const k in items) { localStorage.setItem(k, items[k]); } }\n"
        "  } catch (e) { /* 隐私模式等场景下读不到 localStorage，忽略 */ }\n"
        "})();"
    )
