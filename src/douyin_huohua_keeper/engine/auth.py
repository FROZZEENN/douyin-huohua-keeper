"""登录态检查。

Cookie 过期是这类自动化最确定会发生的事 —— 抖音的 ``sessionid`` 只有
7~30 天寿命。问题不在于「会不会过期」，而在于**过期时你能不能第一时间知道**。

三种检测手段，成本递增，精度也递增：

1. ``check_cookie_freshness`` —— 纯本地，看登录态文件里 cookie 的过期时间。
   零成本，可以在每次运行前跑。缺点是抖音可能提前作废服务端会话。
2. ``probe_chat_page`` —— 打开会话页，看是否被重定向到登录页。
   成本是一次页面加载，但这是最接近「现在真的能发消息吗」的判断。
3. ``verify_send_ready`` —— 在真正发送前调用，检查输入框等关键元素是否存在。
   成本最高，但失败的代价也最高（发出去一半失败更麻烦）。

三层都用上：启动时用 1，任务开始时用 2，每个收件人发送前用 3。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

# 抖音登录态里最关键的那个 cookie
SESSION_COOKIE = "sessionid"

# 保守估计的寿命。实测在 7~30 天之间波动，按短的算 ——
# 提前提醒你重登，好过某天突然发现已经断了三天
CONSERVATIVE_LIFETIME_DAYS = 7.0

# 剩余不到这么多天就提示「该重登了」。
#
# ⚠️ 必须**小于** CONSERVATIVE_LIFETIME_DAYS。以前这里写的是 14 天，
# 而按文件年龄估出来的 remaining 恒 ≤ 7（7 天保守寿命 − 文件年龄），
# 于是 needs_renewal 永远为 True、「登录态正常」这条分支永远不可达 ——
# 界面会无休止地催你重扫码（实测踩过）。
RENEW_REMINDER_DAYS = 3.0


@dataclass(frozen=True, slots=True)
class CookieStatus:
    """本地登录态的时效性判断结果。"""

    present: bool
    session_present: bool
    file_age_days: float | None
    expires_in_days: float | None
    expires_at: str | None
    needs_renewal: bool
    expired: bool
    detail: str

    @property
    def healthy(self) -> bool:
        return self.present and self.session_present and not self.expired


@dataclass(frozen=True, slots=True)
class LoginCheck:
    """一次登录态有效性检查的结果。"""

    ok: bool
    reason: str
    detail: str = ""
    redirected_to_login: bool = False
    checked_at: str = ""

    def __post_init__(self) -> None:
        if not self.checked_at:
            object.__setattr__(self, "checked_at", time.strftime("%Y-%m-%dT%H:%M:%S"))


def check_cookie_freshness(state_path: Path) -> CookieStatus:
    """纯本地检查登录态文件。

    不发起任何网络请求，所以在启动时、工作台轮询时都可以放心调用。

    判断依据有两个，取更悲观的那个：
    - 文件最后修改时间（每次成功刷新登录态都会重写文件）
    - cookie 自带的 expires 字段（如果有的话）
    """
    state_path = Path(state_path)

    if not state_path.is_file():
        return CookieStatus(
            present=False,
            session_present=False,
            file_age_days=None,
            expires_in_days=None,
            expires_at=None,
            needs_renewal=True,
            expired=True,
            detail="尚无登录态，需要在工作台扫码绑定",
        )

    file_age_days = (time.time() - state_path.stat().st_mtime) / 86400

    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CookieStatus(
            present=True,
            session_present=False,
            file_age_days=file_age_days,
            expires_in_days=None,
            expires_at=None,
            needs_renewal=True,
            expired=True,
            detail=f"登录态文件无法解析（{type(exc).__name__}），需要重新扫码",
        )

    session = _find_cookie(payload, SESSION_COOKIE)

    if session is None:
        return CookieStatus(
            present=True,
            session_present=False,
            file_age_days=file_age_days,
            expires_in_days=None,
            expires_at=None,
            needs_renewal=True,
            expired=True,
            detail=f"登录态里没有 {SESSION_COOKIE}，可能保存不完整，需要重新扫码",
        )

    expires_at, expires_in_days = _cookie_expiry(session)

    # 文件年龄换算成「剩余天数」：假设 cookie 有 CONSERVATIVE_LIFETIME_DAYS 的寿命
    remaining_by_age = CONSERVATIVE_LIFETIME_DAYS - file_age_days

    candidates = [remaining_by_age]
    if expires_in_days is not None:
        candidates.append(expires_in_days)
    remaining = min(candidates)

    expired = remaining <= 0
    needs_renewal = remaining <= RENEW_REMINDER_DAYS

    if expired:
        detail = f"登录态已过期（约 {abs(remaining):.0f} 天前失效），需要重新扫码"
    elif needs_renewal:
        detail = f"登录态剩余约 {remaining:.0f} 天，建议尽快重新扫码"
    else:
        detail = f"登录态正常，剩余约 {remaining:.0f} 天"

    return CookieStatus(
        present=True,
        session_present=True,
        file_age_days=file_age_days,
        expires_in_days=remaining,
        expires_at=expires_at,
        needs_renewal=needs_renewal,
        expired=expired,
        detail=detail,
    )


def probe_chat_page(page: Any, *, timeout_ms: int = 30_000) -> LoginCheck:
    """打开会话页，判断登录态是否还有效。

    这是一个**有副作用**的检查（会导航页面），所以只在任务开始时调用，
    不要塞进每个收件人的循环里。

    职责划分：这里只负责**导航**，以及导航本身失败 / 被重定向到登录页的
    处理；页面内容的判定交给 ``verify_chat_page`` —— 它会**轮询等待**
    会话列表渲染出来。

    为什么必须这样拆：``goto`` 用的是 ``wait_until="domcontentloaded"``，
    它在会话列表渲染之前就返回了。以前这里导航完立刻同步检查一次标志位，
    于是稳定地误判成「页面已加载但无法识别会话页结构」。
    """
    try:
        page.goto("https://www.douyin.com/chat", wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as exc:  # noqa: BLE001
        return LoginCheck(
            ok=False,
            reason="NAVIGATION_FAILED",
            detail=f"打开会话页失败：{type(exc).__name__}: {exc}",
        )

    # 被重定向到登录页 —— 这个判据最硬，先判
    current_url = (page.url or "").lower()
    if any(marker in current_url for marker in ("/login", "passport", "sso")):
        return LoginCheck(
            ok=False,
            reason="AUTH_EXPIRED",
            detail="被重定向到登录页，登录态已失效",
            redirected_to_login=True,
        )

    # 剩下的交给会轮询的版本。至少留 20 秒等会话列表渲染。
    return verify_chat_page(page, timeout_ms=max(timeout_ms // 2, 20_000))


def verify_chat_page(page: Any, *, timeout_ms: int = 20_000) -> LoginCheck:
    """轻量检查：当前页面还是不是一个可用的会话页。**不导航、无副作用。**

    为什么需要它、以及为什么不能用 ``verify_send_ready`` 代替：

    **消息输入框只有在打开某个会话之后才会出现。** 实测（抖音 2026-09）：
    刚进 ``/chat`` 时页面上只有搜索框和一列会话行，
    **一个 contenteditable 都没有**；点开某个会话后，
    ``div[contenteditable="true"]``（placeholder='发送消息'）才出现。

    所以「打开会话之前先检查输入框」这个顺序是错的 ——
    必然报 COMPOSER_MISSING，看起来像「登录态失效」，其实只是还没打开会话。

    ``timeout_ms`` 会**轮询等待**，这是必须的：导航用的是
    ``wait_until="domcontentloaded"``，它在会话列表渲染出来之前就返回了。
    只做一次即时检查会稳定地误判成「认不出会话页结构」。
    （这个坑在二维码定位上踩过一次，这里是同一类问题。）
    """
    import time as _time

    from .selectors import CHAT_PAGE_MARKERS, LOGIN_PAGE_MARKERS

    deadline = _time.monotonic() + timeout_ms / 1000.0 if timeout_ms > 0 else None

    while True:
        # 页面上出现登录元素 → 登录态确实失效了。这个要**优先判**，
        # 因为它是一个明确的否定信号，不需要等。
        for selector in LOGIN_PAGE_MARKERS:
            try:
                if page.locator(selector).count() > 0:
                    return LoginCheck(
                        ok=False,
                        reason="AUTH_EXPIRED",
                        detail="页面上出现了登录相关元素，登录态已失效",
                        redirected_to_login=True,
                    )
            except Exception:  # noqa: BLE001
                continue

        # 能看到会话页结构 → 可用
        for selector in CHAT_PAGE_MARKERS:
            try:
                if page.locator(selector).count() > 0:
                    return LoginCheck(ok=True, reason="OK", detail="会话页结构正常")
            except Exception:  # noqa: BLE001
                continue

        if deadline is None or _time.monotonic() >= deadline:
            break

        # 还没渲染出来，等一小会儿再来一轮
        _time.sleep(0.5)

    return LoginCheck(
        ok=False,
        reason="UNKNOWN_PAGE",
        detail=(
            "认不出会话页结构。可能页面还没加载完，也可能抖音前端改版了。"
            "建议先用 --headed 模式看看到底渲染了什么。"
        ),
    )


def verify_send_ready(page: Any) -> LoginCheck:
    """发送前的最后一道检查：输入框真的在吗。

    ⚠️ **必须在「已经打开目标会话」之后调用。**

    输入框是跟着会话详情一起出现的：没打开任何会话时页面上根本没有它。
    如果在打开会话之前调用，会稳定地收到 ``COMPOSER_MISSING``，
    被上层误判成「登录态失效」—— 这正是实测踩过的坑。

    用途：长时间的页面操作后可能会掉登录，宁可在这里失败，
    也不要「按了回车但其实什么都没输入」。
    """
    from .selectors import COMPOSER_INPUT

    try:
        locator = page.locator(COMPOSER_INPUT).first
        if locator.count() == 0:
            return LoginCheck(
                ok=False,
                reason="COMPOSER_MISSING",
                detail=(
                    "找不到消息输入框。若此时尚未打开任何会话，这是正常的 —— "
                    "输入框要打开会话后才出现；若已经打开了会话，"
                    "则可能是登录态已失效或页面结构变了。"
                ),
            )
        if not locator.is_visible():
            return LoginCheck(
                ok=False,
                reason="COMPOSER_HIDDEN",
                detail="消息输入框不可见，页面可能还在加载或已经掉登录",
            )
    except Exception as exc:  # noqa: BLE001
        return LoginCheck(
            ok=False,
            reason="COMPOSER_CHECK_FAILED",
            detail=f"检查输入框时出错：{type(exc).__name__}: {exc}",
        )

    return LoginCheck(ok=True, reason="OK", detail="输入框就绪")


# =============================================================================
# 内部辅助
# =============================================================================


def _find_cookie(payload: dict[str, Any], name: str) -> dict[str, Any] | None:
    """在 storage_state 里找指定名字的 cookie。

    Playwright 的 storage_state 结构是 ``{"cookies": [...], "origins": [...]}``，
    但为了兼容直接把 cookie 列表放在顶层的写法，两种都试。
    """
    cookies = payload.get("cookies")
    if not isinstance(cookies, list):
        cookies = payload if isinstance(payload, list) else []

    for cookie in cookies:
        if isinstance(cookie, dict) and cookie.get("name") == name:
            return cookie
    return None


def _cookie_expiry(cookie: dict[str, Any]) -> tuple[str | None, float | None]:
    """从 cookie 里算出过期时间和剩余天数。

    抖音的 sessionid 往往**不带** expires 字段（会话级 cookie），
    所以这里返回 None 是常见情况，调用方要靠文件年龄来估算。
    """
    raw = cookie.get("expires")
    if not isinstance(raw, (int, float)) or raw <= 0:
        return None, None

    remaining_days = (raw - time.time()) / 86400
    expires_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(raw))
    return expires_at, remaining_days


def summarize_state(state_path: Path) -> dict[str, Any]:
    """给工作台用的登录态摘要。字段名直接对应前端展示。"""
    status = check_cookie_freshness(state_path)
    return {
        "present": status.present,
        "healthy": status.healthy,
        "session_present": status.session_present,
        "file_age_days": round(status.file_age_days, 1) if status.file_age_days is not None else None,
        "expires_in_days": round(status.expires_in_days, 1) if status.expires_in_days is not None else None,
        "expires_at": status.expires_at,
        "needs_renewal": status.needs_renewal,
        "expired": status.expired,
        "detail": status.detail,
    }
