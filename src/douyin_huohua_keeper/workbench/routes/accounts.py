"""账号与扫码登录。

这是整个工作台**最需要小心**的一组接口 —— 它返回的东西（二维码）能让任何
拿到它的人登进你的抖音账号。

因此：
- 二维码只在一段有限的会话里有效，用完立刻清掉
- 登出（删除登录态文件）是不可逆操作，必须显式确认
- 所有返回体里**绝不含** cookie 值本身，只含「有没有、还新鲜吗」

扫码流程的时序：

1. ``POST /api/accounts/qr/start``    启动扫码会话，返回二维码图片
2. ``GET  /api/accounts/qr/poll``     前端每秒轮询一次，直到 success/expired
3. ``POST /api/accounts/qr/cancel``   用户关掉弹窗时调，释放浏览器
4. ``DELETE /api/accounts/state``     登出（清空登录态文件）

关于「没有账号时也要能正常工作」：除了 ``qr/start`` 会真正启动浏览器之外，
其余接口在无登录态时都返回清晰的 ``present: false`` 状态而不是报错。
前端据此展示「还没扫码绑定」的引导。
"""

from __future__ import annotations

import base64
import contextlib
import logging
import os
import time
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ...engine.auth import summarize_state
from ...store.repo import now_str, safe_id

LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

# 扫码会话的有效期。抖音二维码大概 2 分钟失效，这里留点余量。
QR_SESSION_TTL_SECONDS = 300.0

# 登录页截图的 JPEG 画质。
#
# 这张图会回传给浏览器 —— **是出方向流量**（按流量计费的云主机要为它付钱）。
# 实测 q=60 时单张约 88KB；调到 45 后肉眼几乎无差别，但体积明显更小。
# 配合前端 15 秒一刷，中继页面的流量能降到原来的 1/5 左右。
_SHOT_JPEG_QUALITY = 45


def _state(request: Request) -> Any:
    return request.app.state.keeper


def _default_account_id(repo: Any) -> str:
    """当前使用的账号 ID。

    只支持单账号：用第一个有登录态的文件名，没有就固定叫 ``main``。
    多账号是未来的事，现在不要提前复杂化。
    """
    accounts = repo.list_accounts()
    return accounts[0] if accounts else "main"


# =============================================================================
# 账号状态
# =============================================================================


@router.get("")
@router.get("/")
async def list_accounts(request: Request) -> dict[str, Any]:
    """列出账号及其登录态摘要。

    前端首屏就用这个接口 —— 所以它必须**在没有任何登录态时也能正常返回**，
    不能抛异常，否则用户看到的是一片空白而不是「去扫码」的引导。
    """
    state = _state(request)
    repo = state.repository

    account_id = _default_account_id(repo)
    summary = summarize_state(repo.account_state_path(account_id))

    return {
        "accounts": [
            {
                "id": account_id,
                "label": "主账号",
                "state": summary,
            }
        ],
        "has_any_state": bool(repo.list_accounts()),
        "stored_ids": list(repo.list_accounts()),
    }


@router.get("/state")
async def account_state(request: Request) -> dict[str, Any]:
    """当前账号的登录态摘要。

    纯本地检查，零网络开销 —— 前端可以放心定时刷新。
    """
    state = _state(request)
    repo = state.repository
    account_id = _default_account_id(repo)

    summary = summarize_state(repo.account_state_path(account_id))
    summary["account_id"] = account_id
    summary["checked_at"] = now_str()

    # 浏览器是否已经在跑 —— 前端据此显示「浏览器已就绪」
    summary["engine_running"] = state.peek_engine() is not None

    return summary


@router.delete("/state")
async def clear_account_state(request: Request, account_id: str | None = None) -> dict[str, Any]:
    """登出：删掉登录态文件。

    只删这一个文件，不碰其他任何数据。返回删除前后的状态，
    让前端能立刻更新界面而不必再发一次请求。
    """
    state = _state(request)
    repo = state.repository

    target = account_id or _default_account_id(repo)
    # account_id 直接来自 query，会被拼进文件路径 —— 必须在入口挡住路径穿越。
    # （Repository 里还有最后一道 safe_id，这里只是为了让返回码是 400 而不是 500。）
    try:
        target = safe_id(target, kind="账号 ID")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    removed = repo.delete_account_state(target)

    LOGGER.warning("已删除账号 %s 的登录态（用户主动登出）", target)

    return {
        "removed": removed,
        "account_id": target,
        "detail": (f"已删除 {target} 的登录态文件" if removed else f"账号 {target} 本来就没有登录态"),
        "state": summarize_state(repo.account_state_path(target)),
    }


# =============================================================================
# 扫码登录
# =============================================================================


class QrStartRequest(BaseModel):
    """启动扫码会话的参数。"""

    account_id: str | None = Field(default=None, description="留空表示当前主账号")
    force_new: bool = Field(
        default=False,
        description="即使已有扫码会话也重新开一个（用于「刷新二维码」）",
    )


@router.post("/qr/start")
def qr_start(request: Request, payload: QrStartRequest | None = None) -> dict[str, Any]:
    """启动扫码会话并返回二维码。

    注意这个接口会**真正启动浏览器**（首次要几秒），所以前端的按钮文案
    应该写成「正在启动浏览器…」而不是「正在加载」。

    同一个时刻只允许一个扫码会话 —— 多个并发会话会让浏览器里开着多个
    登录页，而它们共享同一份 cookie，结果不可预测。
    """
    payload = payload or QrStartRequest()
    state = _state(request)
    repo = state.repository

    account_id = payload.account_id or _default_account_id(repo)
    # 同 clear_account_state：来自请求体的 account_id 会被拼进状态文件路径，
    # 而且**登录成功时会往里写文件** —— 不校验就等于任意写。
    try:
        account_id = safe_id(account_id, kind="账号 ID")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # --- 第一段（持锁，但很快）：决定是复用还是新建 ---
    reuse = False
    with state.qr_lock:
        existing = state.qr_session
        if existing is not None and not payload.force_new and not _session_expired(existing):
            reuse = True
            account_id = existing.get("account_id") or account_id

    # --- 第二段（不持锁，可能几秒）：真正的浏览器操作 ---
    # 刻意把慢操作放在锁外 —— 启动 Chromium 要几秒，持锁会让并发的
    # /qr/poll 全部堵在这里，前端看起来就是「卡住了」。
    # 并发保护靠下面的「复核 + 落定」而不是靠长时间持锁。
    if reuse:
        try:
            qr = _grab_qrcode(state, navigate=False)
            with state.qr_lock:
                if state.qr_session is not None:
                    return _qr_payload(qr, account_id, reused=True)
            # 会话在截图期间被取消了 —— 落到下面走新建流程
        except Exception as exc:  # noqa: BLE001
            LOGGER.info("复用扫码会话失败，改为新建：%s", exc)

    with state.qr_lock:
        _close_session(state)

    try:
        qr = _grab_qrcode(state, navigate=True)
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("获取二维码失败：%s", exc)
        return {
            "ok": False,
            "account_id": account_id,
            "error": "qrcode_unavailable",
            "message": str(exc),
            "hint": (
                "常见原因：服务器访问不了抖音（网络/DNS）、Chromium 未安装"
                "（执行 playwright install chromium chromium-headless-shell —— 两个都要）、或抖音改了登录页结构。"
            ),
        }

    with state.qr_lock:
        state.qr_session = {
            "account_id": account_id,
            "started_at": time.time(),
            "started_at_str": now_str(),
            "last_state": "awaiting_scan",
            "saved": False,
        }

    return _qr_payload(qr, account_id, reused=False)


# ⚠️ 下面这几个路由里都是**阻塞调用**（Playwright 同步 API / 真实 HTTP），
# 刻意写成 `def` 而不是 `async def` —— FastAPI 会把同步路由丢进线程池，
# 事件循环就不会被卡住。改回 `async` 的话，扫码轮询期间整个工作台会冻结。
@router.get("/qr/poll")
def qr_poll(request: Request) -> dict[str, Any]:
    """轮询一次扫码进度。

    设计成「单次快速返回、由前端按秒轮询」，而不是后端阻塞等待 ——
    这样用户关掉页面就自然停了，不会留下一个后台在跑的循环。
    """
    state = _state(request)

    with state.qr_lock:
        session = state.qr_session
        if session is None:
            return {
                "state": "idle",
                "detail": "没有进行中的扫码会话",
                "ok": False,
            }

        if _session_expired(session):
            _close_session(state)
            return {
                "state": "expired",
                "detail": "扫码会话已超时，请重新获取二维码",
                "ok": False,
            }

        if session.get("saved"):
            return {
                "state": "success",
                "detail": "登录态已保存",
                "ok": True,
                "account_id": session.get("account_id"),
            }

    # 浏览器操作放锁外做 —— 单次只有几秒，但不应阻塞 qr/start
    try:
        from ...engine import qrlogin

        engine = state.get_engine()
        poll = engine.session.call(lambda ctx: qrlogin.poll_login(ctx.page))

        with state.qr_lock:
            session = state.qr_session
            if session is not None:
                session["last_state"] = poll.state.value

        if poll.state is qrlogin.LoginState.SUCCESS:
            return _finalize_login(state, detail=poll.detail)

        if poll.state is qrlogin.LoginState.EXPIRED:
            return {
                "state": "expired",
                "detail": poll.detail or "二维码已失效，请刷新",
                "ok": False,
            }

        return {
            "state": poll.state.value,
            "detail": poll.detail,
            "ok": True,
        }

    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("轮询扫码状态失败：%s", exc)
        # ⚠️ 这里返回 `error` 曾经是个**致命的自伤**：前端一看到 error 就
        # `stopQrPoll()`，于是**一次**瞬时失败（浏览器忙、操作超时、一次网络抖动）
        # 就把整个轮询永久停掉了 —— 界面永远停在「等待扫码」，
        # 用户扫了码、也确认了，却再也等不到任何反应（实测踩过）。
        #
        # 单次检查失败是**可重试**的，不是终态。返回 `retrying`，
        # 前端会继续按秒轮询。
        return {
            "state": "retrying",
            "detail": f"这一轮检查没成功（{type(exc).__name__}），正在自动重试…",
            "ok": False,
        }


@router.post("/qr/cancel")
def qr_cancel(request: Request) -> dict[str, Any]:
    """放弃当前扫码会话，释放浏览器。

    用户关掉二维码弹窗时前端会调这个 —— 不调的话浏览器会一直开着
    （虽然有 TTL 兜底，但不该指望它）。
    """
    state = _state(request)

    with state.qr_lock:
        had = state.qr_session is not None
        _close_session(state)

    return {
        "ok": True,
        "cancelled": had,
        "detail": "已放弃扫码" if had else "本来就没有进行中的扫码",
    }


# =============================================================================
# 登录页「远程操作」中继
#
# 存在的理由：账号在新设备 / 新 IP 上登录时，抖音经常要求二次验证
# （「选择验证方式」→ 手机号 / 短信 → 填验证码）。这个页面在无头浏览器里
# **既看不见也点不到**，于是用户会卡在「手机确认了但一直登不进去」。
#
# 这两个接口把登录页「搬」到工作台上：看截图 + 点一下 + 输一下。
# 它只是把用户本来在自己电脑上要做的事，搬到了远程浏览器里 ——
# 不是绕过任何验证。
# =============================================================================


@router.get("/qr/page")
def qr_page(request: Request) -> dict[str, Any]:
    """截一张登录页当前的样子（可视区域），供前端「远程操作」面板展示。"""
    state = _state(request)

    with state.qr_lock:
        if state.qr_session is None:
            return {"ok": False, "detail": "没有进行中的扫码会话"}

    try:
        engine = state.get_engine()
        info = engine.session.call(_capture_page, timeout=25)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("截取登录页失败：%s", exc)
        return {"ok": False, "detail": f"截取登录页失败：{type(exc).__name__}: {exc}"}

    return {"ok": True, **info}


class QrActRequest(BaseModel):
    """在登录页上做一次操作。"""

    action: Literal["click", "type", "key", "reload", "focus", "clear"]
    x: float | None = Field(default=None, description="点击坐标 x（可视区域像素）")
    y: float | None = Field(default=None, description="点击坐标 y（可视区域像素）")
    text: str | None = Field(default=None, max_length=200, description="type 要输入的文字")
    key: str | None = Field(default=None, max_length=40, description="key 要按的键，如 Enter")
    index: int | None = Field(
        default=None,
        ge=0,
        le=20,
        description=(
            "要操作的输入框序号（与 /qr/page 返回的 inputs 顺序一致）。留空时自动选「最像验证码」的那个框。"
        ),
    )


@router.post("/qr/act")
def qr_act(request: Request, payload: QrActRequest) -> dict[str, Any]:
    """在登录页上执行一次操作（点击 / 输入 / 聚焦 / 清空 / 按键 / 刷新），并回一张新截图。

    返回体里带 ``inputs``（页面上各输入框的当前内容）—— 这是给用户的
    **可见反馈**：填完验证码能立刻看到「字确实进去了」，而不是「点了没反应」。
    """
    state = _state(request)

    with state.qr_lock:
        if state.qr_session is None:
            raise HTTPException(status_code=409, detail="没有进行中的扫码会话")
        last_click = state.qr_session.get("last_click")

    engine = state.get_engine()

    try:
        result = engine.session.call(
            lambda ctx: _apply_action(ctx.page, payload, last_click=last_click), timeout=30
        )
    except Exception as exc:
        LOGGER.warning("登录页操作失败：%s", exc)
        raise HTTPException(status_code=502, detail=f"操作登录页失败：{type(exc).__name__}: {exc}") from exc

    detail = result.get("detail", "") if isinstance(result, dict) else ""
    new_last_click = result.get("last_click") if isinstance(result, dict) else None

    with state.qr_lock:
        if state.qr_session is not None:
            state.qr_session["last_click"] = new_last_click

    out: dict[str, Any] = {"ok": True, "detail": detail}
    # ⚠️ 必须把动作的**反馈字段**一起转发出去。
    # 曾经漏了 `verified` —— 前端拿不到写没写成功的结论，界面就无法报错，
    # 于是「其实没写进去」和「写进去了」在用户眼里一模一样。
    if isinstance(result, dict):
        for key in ("verified", "actual", "target", "focused"):
            if key in result:
                out[key] = result[key]

    # 操作完顺手回一张新截图，前端不必再单独请求一次
    try:
        out.update(engine.session.call(_capture_page, timeout=25))
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("操作后重新截图失败：%s", exc)
        out["detail"] = f"{detail}（重新截图失败：{type(exc).__name__}）"

    return out


# =============================================================================
# 「可输入框」的枚举 / 定位 / 写入
#
# ⚠️ 这三个 JS **必须共用同一份筛选条件**（`_EDITABLE_INPUTS`），否则
#    「读出来的第 3 个」和「写进去的第 3 个」会不是同一个元素 —— 那正是
#    一次真实事故的成因：用户点「输入内容」，字写进了页面上**第一个**
#    可见输入框（抖音登录页第一个是「+86」国家码），于是「点了没用」。
#
#    所以现在的做法是：用同一个筛选函数枚举 → 给目标框打标记
#    `data-huohua-target` → 再用普通选择器精确定位它。不靠猜、也不靠坐标。
# =============================================================================

# 枚举页面上**可见**的可编辑输入框（顺序即 index，前后端共用这个顺序）
_READ_INPUTS_JS = """
() => {
  const SKIP = new Set(['hidden', 'checkbox', 'radio', 'submit', 'button', 'file']);
  const els = Array.from(document.querySelectorAll('input, textarea, [contenteditable="true"]'))
    .filter((el) => {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (SKIP.has(type)) return false;
      const r = el.getBoundingClientRect();
      return r.width >= 2 && r.height >= 2;
    });
  return els.slice(0, 8).map((el) => ({
    type: (el.getAttribute('type') || 'text').toLowerCase(),
    placeholder: String(el.getAttribute('placeholder') || '').slice(0, 30),
    value: String(el.isContentEditable ? (el.innerText || '') : (el.value || '')).slice(0, 40),
  }));
}
"""

# 挑出「最像验证码输入框」的那个，返回它的 index
#
# ⚠️ 这段判据踩过一次真实的坑，改之前务必看懂：
#
#   抖音登录页上同时存在三个可见输入框：
#       [0] 国家码（值是「+86」，**没有 placeholder**，但 id/name 里含 "code"）
#       [1] 请输入手机号
#       [2] 请输入验证码
#
#   最初的实现只按关键词 `code` 匹配 id/name —— 于是**国家码框被选中**（它含 "code"），
#   验证码被写进了国家码框（还被 maxlength 截断）。用户看到的就是「点了没用」。
#
#   所以现在的规则是「**先排除，再匹配**」：
#   1. 明确带「国家码」特征的框直接排除（id/name 含 country/area/zone/dial/region，
#      或值形如 ``+86``）—— 值形如 ``+数字`` 是最可靠的信号；
#   2. 剩下里挑**明确提到验证码**的（验证码/校验码/verify/vcode/otp/captcha…）；
#   3. 都没有就退回「排除国家码后最靠后的那个」（登录页上验证码框排在手机号之后）。
_PICK_CODE_INPUT_JS = """
() => {
  const SKIP = new Set(['hidden', 'checkbox', 'radio', 'submit', 'button', 'file']);
  const els = Array.from(document.querySelectorAll('input, textarea, [contenteditable="true"]'))
    .filter((el) => {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (SKIP.has(type)) return false;
      const r = el.getBoundingClientRect();
      return r.width >= 2 && r.height >= 2;
    });

  const hay = (el) => [
    el.getAttribute('placeholder') || '',
    el.getAttribute('aria-label') || '',
    el.getAttribute('name') || '',
    el.id || '',
  ].join(' ').toLowerCase();

  // 国家/地区码框：id/name 特征 或 值就是「+数字」
  const COUNTRY = ['country', 'area', 'zone', 'dial', 'region', '国家', '区号'];
  const isCountry = (el) => {
    if (COUNTRY.some((k) => hay(el).includes(k))) return true;
    const value = String(el.value || '').trim();
    return /^\\+\\d*$/.test(value);
  };

  const CODE = ['验证码', '校验码', '动态码', '短信', 'verify', 'vcode', 'authcode', 'otp', 'captcha', 'smscode'];
  for (let i = 0; i < els.length; i++) {
    if (isCountry(els[i])) continue;
    if (CODE.some((k) => hay(els[i]).includes(k))) return i;
  }

  for (let i = els.length - 1; i >= 0; i--) {
    if (!isCountry(els[i])) return i;
  }
  return null;
}
"""

# 给第 index 个可见可编辑输入框打上标记，之后就能用普通选择器精确定位它
_MARK_INPUT_JS = """
(index) => {
  const SKIP = new Set(['hidden', 'checkbox', 'radio', 'submit', 'button', 'file']);
  const els = Array.from(document.querySelectorAll('input, textarea, [contenteditable="true"]'))
    .filter((el) => {
      const type = (el.getAttribute('type') || 'text').toLowerCase();
      if (SKIP.has(type)) return false;
      const r = el.getBoundingClientRect();
      return r.width >= 2 && r.height >= 2;
    });
  els.forEach((el) => el.removeAttribute('data-huohua-target'));
  if (index === null || index < 0 || index >= els.length) return null;
  const el = els[index];
  el.setAttribute('data-huohua-target', '1');
  return {
    index: index,
    placeholder: String(el.getAttribute('placeholder') || ''),
    value: String(el.isContentEditable ? (el.innerText || '') : (el.value || '')),
  };
}
"""

# 看「当前焦点落在哪个元素上」，用来判断刚才那一下点的是不是可输入的框。
_FOCUSED_INFO_JS = """
() => {
  const el = document.activeElement;
  if (!el) return null;
  const tag = (el.tagName || '').toLowerCase();
  const editable = tag === 'input' || tag === 'textarea' || el.isContentEditable === true;
  return {
    tag: tag,
    editable: editable,
    type: (el.getAttribute && el.getAttribute('type')) || '',
    placeholder: (el.getAttribute && el.getAttribute('placeholder')) || '',
  };
}
"""


def _capture_page(ctx: Any) -> dict[str, Any]:
    """截一张登录页的可视区域（不是整页 —— 要让前端坐标能直接对上）。

    用 **JPEG** 而不是 PNG：同一张 1440x900 的图，PNG 约 300KB+，
    JPEG 只有几十 KB —— 手机上的体感差别很大（PNG 会明显卡顿、也更容易把
    浏览器线程占住）。

    ⚠️ 画质和刷新频率都是**流量成本**的直接乘数：这张图会以 base64 走接口
    回传给浏览器，也就是**出方向流量**（按流量计费的云主机会为它给钱）。
    所以 q=45 + 前端 15 秒一刷，是"看得清"和"不烧流量"之间的折中 ——
    别再随手调回 60/5 秒了，除非你不在意账单。
    """
    page = ctx.page
    shot = page.screenshot(type="jpeg", quality=_SHOT_JPEG_QUALITY, timeout=15_000)
    viewport = page.viewport_size or {"width": 1440, "height": 900}

    inputs: list[dict[str, Any]] = []
    with contextlib.suppress(Exception):
        inputs = page.evaluate(_READ_INPUTS_JS) or []

    code_index: int | None = None
    with contextlib.suppress(Exception):
        code_index = page.evaluate(_PICK_CODE_INPUT_JS)

    return {
        "url": page.url,
        "width": int(viewport.get("width") or 1440),
        "height": int(viewport.get("height") or 900),
        "image": "data:image/jpeg;base64," + base64.b64encode(shot).decode("ascii"),
        "inputs": inputs,
        # 前端用它做下拉框的默认选项（最像验证码的那个框）
        "code_index": code_index,
    }


def _read_inputs(page: Any) -> list[dict[str, Any]]:
    with contextlib.suppress(Exception):
        return page.evaluate(_READ_INPUTS_JS) or []
    return []


def _focused_info(page: Any) -> dict[str, Any] | None:
    with contextlib.suppress(Exception):
        return page.evaluate(_FOCUSED_INFO_JS)
    return None


def _input_value(locator: Any) -> str:
    """读一个输入框当前的值（contenteditable 没有 input_value，退回 inner_text）。"""
    try:
        return str(locator.input_value() or "")
    except Exception:  # noqa: BLE001
        pass
    try:
        return str(locator.inner_text() or "")
    except Exception:  # noqa: BLE001
        return ""


def _target_input(page: Any, index: int | None) -> tuple[Any, dict[str, Any] | None]:
    """把「这次要操作的那个输入框」标记出来，并返回能精确定位它的 locator。

    ``index`` 为空时自动挑「最像验证码」的那个框。

    为什么不用「点坐标」来聚焦：用户点的是**截图**，坐标要经过缩放换算，
    而且页面上元素会动；一旦偏几个像素点到了别处，字就写飞了。
    用 JS 枚举 + 打标记 + 选择器定位，是**确定性**的。
    """
    target_index = index
    if target_index is None:
        with contextlib.suppress(Exception):
            target_index = page.evaluate(_PICK_CODE_INPUT_JS)

    info: dict[str, Any] | None = None
    if target_index is not None:
        with contextlib.suppress(Exception):
            info = page.evaluate(_MARK_INPUT_JS, target_index)

    if not info:
        return None, None

    locator = page.locator('[data-huohua-target="1"]').first
    try:
        if locator.count() <= 0:
            return None, info
    except Exception:  # noqa: BLE001
        return None, info
    return locator, info


def _write_input(page: Any, locator: Any, text: str) -> tuple[bool, str, str]:
    """往指定输入框写内容，并**回读确认**；返回 ``(是否成功, 实际值, 用的方式)``。

    依次尝试三种方式，每种都回读一次：

    1. ``fill`` —— 直接设值并派发 input 事件，**不依赖焦点**，最稳；
    2. 点一下再逐字敲（有些组件只认按键事件）；
    3. ``insert_text`` 整体插入。

    ⚠️ 必须回读：以前是「调完就报『已输入』」，结果字根本没进去，
    用户看到的是「提示输入了、但页面上什么都没有」—— 假反馈比没反馈更糟。
    """
    for label, writer in (
        ("直接填入", lambda: locator.fill(text)),
        ("点击后输入", lambda: (locator.click(timeout=5_000), page.keyboard.type(text, delay=60))),
        ("整体插入", lambda: (locator.click(timeout=5_000), page.keyboard.insert_text(text))),
    ):
        with contextlib.suppress(Exception):
            writer()
            page.wait_for_timeout(250)
            value = _input_value(locator)
            if text and text in value:
                return True, value, label

    return False, _input_value(locator), "三种方式都没写进去"


def _apply_action(page: Any, payload: QrActRequest, *, last_click: Any) -> dict[str, Any]:
    """把前端传来的一次操作落到页面上，并回一些「看得见的反馈」。

    返回值里 ``last_click`` 会在「点到可输入框」时被记下，
    ``inputs`` 是页面输入框的当前内容 —— 让用户能确认字真的进去了。
    """
    action = payload.action

    if action == "reload":
        with contextlib.suppress(Exception):
            page.reload(wait_until="domcontentloaded", timeout=20_000)
        page.wait_for_timeout(1_500)
        return {"detail": "已刷新页面", "last_click": None}

    if action == "click":
        if payload.x is None or payload.y is None:
            raise ValueError("click 需要 x 和 y 坐标")
        x, y = float(payload.x), float(payload.y)
        page.mouse.click(x, y)
        page.wait_for_timeout(300)

        info = _focused_info(page)
        # ⚠️ 只在点到**可输入框**时才记下坐标：否则「上一步点的是『获取验证码』
        # 按钮」，输入时再点一次就会重复发短信（甚至触发限流）。
        editable = bool(info and info.get("editable"))
        return {
            "detail": (
                "已点击，光标已在输入框里 —— 现在可以输入了" if editable else f"已点击 ({x:.0f}, {y:.0f})"
            ),
            "focused": info,
            "last_click": [x, y] if editable else None,
        }

    if action == "focus":
        locator, info = _target_input(page, payload.index)
        if locator is None:
            return {
                "detail": "没找到可输入的框 —— 请先在实时画面上点一下那个输入框",
                "last_click": None,
            }
        with contextlib.suppress(Exception):
            locator.click(timeout=5_000)
        page.wait_for_timeout(150)
        name = (info or {}).get("placeholder") or "输入框"
        return {
            "detail": f"已把光标放进「{name}」，现在可以点「输入内容」了",
            "target": info,
            "last_click": None,
        }

    if action == "clear":
        locator, info = _target_input(page, payload.index)
        if locator is not None:
            with contextlib.suppress(Exception):
                locator.fill("")
                page.wait_for_timeout(150)
                if _input_value(locator):
                    locator.click(timeout=5_000)
                    page.keyboard.press("Control+A")
                    page.keyboard.press("Backspace")
        elif last_click:
            page.mouse.click(float(last_click[0]), float(last_click[1]))
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
        page.wait_for_timeout(150)
        return {"detail": "已清空该输入框", "target": info, "last_click": last_click}

    if action == "type":
        text = payload.text or ""
        if not text:
            raise ValueError("type 需要 text")

        # ★ 关键：写进**指定的那个框**，并回读确认。
        # 不再靠「第一个可见输入框」——那会把验证码写进「+86」国家码框，
        # 用户看到的就是「点了没用」（实测踩过）。
        locator, info = _target_input(page, payload.index)

        if locator is not None:
            ok, actual, how = _write_input(page, locator, text)
        elif last_click:
            # 兜底：用户点过某个框，就点回去再敲
            page.mouse.click(float(last_click[0]), float(last_click[1]))
            page.wait_for_timeout(200)
            with contextlib.suppress(Exception):
                page.keyboard.type(text, delay=60)
            page.wait_for_timeout(250)
            actual, how, ok = "", "点击后输入（未回读）", True
        else:
            ok, actual, how = False, "", "页面上找不到可输入的框"

        if ok:
            detail = f"已写入「{text}」到{(info or {}).get('placeholder') or '输入框'}（{how}）"
        else:
            detail = (
                f"没能写进去（{how}）—— 请先在实时画面上点一下那个输入框，"
                "或在下面的下拉框里选对「要填哪个框」，然后再点「输入内容」"
            )
        return {
            "detail": detail,
            "verified": ok,
            "actual": actual,
            "target": info,
            "last_click": last_click,
        }

    if action == "key":
        page.keyboard.press(payload.key or "Enter")
        page.wait_for_timeout(250)
        return {"detail": "已按键", "last_click": last_click}

    raise ValueError(f"未知操作：{action}")


@router.post("/logout")
def logout(request: Request) -> dict[str, Any]:
    """退出登录：清空登录态并关掉浏览器。

    和 ``DELETE /state`` 的区别：这个还会把浏览器关掉。
    用于「我怀疑这个号登错了」的场景。
    """
    state = _state(request)
    repo = state.repository
    account_id = _default_account_id(repo)

    with state.qr_lock:
        _close_session(state)

    removed = repo.delete_account_state(account_id)

    state.shutdown_engine()

    LOGGER.warning("账号 %s 已登出，浏览器已关闭", account_id)

    return {
        "ok": True,
        "removed": removed,
        "detail": "已登出并关闭浏览器",
        "state": summarize_state(repo.account_state_path(account_id)),
    }


# =============================================================================
# 内部
# =============================================================================


def _grab_qrcode(state: Any, *, navigate: bool) -> Any:
    """抓一张二维码。"""
    from ...engine import qrlogin

    engine = state.get_engine()
    return engine.session.call(lambda ctx: qrlogin.fetch_qrcode(ctx.page, navigate=navigate))


def _qr_payload(qr: Any, account_id: str, *, reused: bool) -> dict[str, Any]:
    return {
        "ok": True,
        "account_id": account_id,
        "image": qr.data_url,
        "mime": qr.mime,
        "expires_hint_seconds": qr.expires_hint_seconds,
        "detail": qr.detail,
        "reused": reused,
        "poll_interval_ms": 1500,
    }


def _finalize_login(state: Any, *, detail: str) -> dict[str, Any]:
    """扫码轮询检测到「已登录」后：**先验证、再落盘**。

    为什么不能直接覆盖正式文件（这是 2026-09 修的一个真实事故）：

    ``save_storage_state`` 只能保证「上下文里有会话 cookie」，而**那不等于
    这份会话真的能用**。实测发生过：扫码后保存「成功」，可是用这份登录态
    打开 /chat 仍然是登录墙 —— 工作台显示「已绑定」，实际所有发送静默失败。

    更糟的是它会**冲掉原本还能用的登录态**：一次失败的重新扫码，把好好的
    会话换成了无效的。

    所以流程改成：

    1. 把当前登录态存到**临时文件** ``<account>.state.json.pending``；
    2. 用这份临时的登录态**新开一个干净上下文**，打开 /chat 验证是否真的可用；
    3. 通过 → 原子替换正式文件；**不通过 → 删掉临时文件，正式文件原封不动**。

    这样「验证不通过」最坏的结果只是「这次登录没成功」，而不会弄坏已有登录态。
    """
    from ...engine import qrlogin

    with state.qr_lock:
        session = state.qr_session
        account_id = (session or {}).get("account_id") or _default_account_id(state.repository)
        already_saved = bool((session or {}).get("saved"))
        last_fail = float((session or {}).get("verify_failed_at") or 0.0)

    if already_saved:
        return {"state": "success", "detail": "登录态已保存", "ok": True, "account_id": account_id}

    # 刚验证失败过就别立刻重来 —— 每次验证都要新开一个浏览器上下文（几十秒），
    # 而前端每 1.5 秒就轮询一次，不挡一下会把它变成死循环。
    if last_fail and (time.time() - last_fail) < 20:
        return {
            "state": "verifying",
            "ok": True,
            "detail": "上一次登录态验证没有通过 —— 请先在「登录页操作」里完成二次验证。",
        }

    repo = state.repository
    path = repo.account_state_path(account_id)
    pending = path.parent / (path.name + ".pending")

    # --- 1. 存到临时文件 ---
    try:
        engine = state.get_engine()
        engine.session.call(lambda ctx: qrlogin.save_storage_state(ctx.page, pending))
    except Exception as exc:  # noqa: BLE001
        _unlink(pending)
        LOGGER.error("保存登录态失败：%s", exc)
        return {
            "state": "error",
            "ok": False,
            "detail": f"保存登录态失败：{type(exc).__name__}: {exc}",
        }

    # --- 2. 真实验证：这份登录态到底能不能用 ---
    try:
        ok, why = engine.session.call(
            lambda ctx: qrlogin.verify_state_works(ctx.browser, pending, timeout_ms=30_000),
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001
        ok, why = False, f"验证登录态时出错：{type(exc).__name__}: {exc}"

    if not ok:
        _unlink(pending)
        with state.qr_lock:
            if state.qr_session is not None:
                state.qr_session["verify_failed_at"] = time.time()
        LOGGER.warning("登录态验证未通过，未覆盖原有登录态：%s", why)
        return {
            "state": "needs_verify",
            "ok": False,
            "account_id": account_id,
            "detail": f"登录还没有真正完成：{why}",
            "hint": (
                "如果页面上出现了「选择验证方式」，请在下方「登录页操作」里"
                "点选验证方式并填写验证码，完成后会自动重试；"
                "否则请点「刷新二维码」重新扫码。原有登录态没有被改动。"
            ),
        }

    # --- 3. 验证通过，原子替换（此刻才动正式文件）---
    try:
        os.replace(pending, path)
    except Exception as exc:  # noqa: BLE001
        _unlink(pending)
        LOGGER.error("替换登录态文件失败：%s", exc)
        return {
            "state": "error",
            "ok": False,
            "detail": f"登录已验证通过，但写入登录态文件失败：{type(exc).__name__}: {exc}",
        }

    with state.qr_lock:
        if state.qr_session is not None:
            state.qr_session["saved"] = True

    summary = summarize_state(path)
    LOGGER.info("扫码登录完成并验证通过，登录态已保存至 %s", path.name)

    # 登录态换了，旧浏览器里的 cookie 已经过期，关掉让下次用新的
    state.shutdown_engine()

    return {
        "state": "success",
        "ok": True,
        "detail": "登录成功并已验证可用，登录态已保存",
        "account_id": account_id,
        "account_state": summary,
        "next": detail,
    }


def _unlink(path: Any) -> None:
    with contextlib.suppress(OSError):
        Path(path).unlink()


def _session_expired(session: dict[str, Any]) -> bool:
    started = session.get("started_at")
    if not isinstance(started, (int, float)):
        return True
    return (time.time() - started) > QR_SESSION_TTL_SECONDS


def _close_session(state: Any) -> None:
    """清掉扫码会话并关掉浏览器。

    关于锁：``threading.Lock`` 不是可重入的，所以这个函数**必须**在
    已经持有 ``qr_lock`` 时调用，且内部不再去拿锁。调用方都在 ``with`` 块里。
    """
    if state.qr_session is None:
        return
    state.qr_session = None
    try:
        state.shutdown_engine()
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("关闭扫码会话的浏览器失败：%s", exc)
