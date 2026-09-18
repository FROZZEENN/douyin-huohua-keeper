"""扫码登录。

云端部署的核心难题：**没有图形界面，怎么让用户扫码？**

解法是把二维码从浏览器里「取」出来交给前端：

1. 无头浏览器打开抖音登录页
2. 等二维码图片渲染出来
3. 把二维码区域的像素截成 PNG，base64 编码
4. 前端把 base64 显示成图片，你手机一扫就行
5. 后端轮询页面，检测到登录成功后立刻把 storage_state 原子落盘

整个交互在浏览器里完成，不需要 SSH、不需要把二维码截图传上服务器。

降级路径：如果登录页把二维码画在 canvas 里（截图可能拿到空白），
就退回截取整个登录弹窗区域。再不行就提示用户开启有头模式 + Xvfb。

⚠️ 这一模块的完整链路需要真实抖音登录页才能验证，
在没有真实账号的情况下只能验证到「能启动、能定位、能截图」这一步。
"""

from __future__ import annotations

import base64
import contextlib
import logging
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from . import selectors as sel

LOGGER = logging.getLogger(__name__)

# 登录页。
#
# 用 /chat 而不是首页 "/"，这是实测踩坑后的结论：
# 首页的登录弹窗是**页面加载时被自动弹出**的，我们截取时二维码区还停在
# 加载态（只有一个抖音 logo 占位），`element.screenshot()` 会拿到一张
# 纯白空图 —— 而且不报错。
#
# /chat 则既会主动弹登录窗、二维码也渲染得完整；而且它本来就是登录成功后
# 的目标页，登录态失效时又会退回同一个弹窗。两种情况共用一个 URL，
# 不需要分支判断。
LOGIN_URL = "https://www.douyin.com/chat"

# 二维码可能出现的地方。**顺序即优先级**，改动前请先读下面这段说明。
#
# 实测确认（抖音 2026-09 的页面）二维码的真实结构是：
#
#     <div id="douyin_login_comp_scan_code">
#       <div id="animate_qrcode_container">
#         <img aria-label="二维码" src="data:image/png;base64,...">
#
# 三条重要经验：
#
# 1. 它的 class 是 ``RhjdbXj8`` 这种**每次构建都会变的哈希名**。
#    绝对不能写进选择器 —— 今天能用，下次改版就静默失效。
#    用 id 和 aria-label 这类语义标识，它们是稳定的。
#
# 2. 二维码是**内联 base64 的 img**，不是 canvas。所以 ``[class*="qrcode"]``
#    这类类名匹配在真实页面上一个都命中不了（抖音没用这些类名）。
#
# 3. 末尾两条宽泛兜底有个真实的坑：登录容器里**第一张 img 是装饰性背景横幅**
#    （flat_bg_2_small.png，726x86），直接取 ``.first`` 会截到那张横幅 ——
#    接口返回成功，但拿到的不是二维码，前端会把它当二维码显示出来。
#    因此 ``_locate_qrcode`` 会对每个候选做**形状校验**，非正方形的直接跳过。
QRCODE_CANDIDATES: tuple[str, ...] = (
    # --- 稳定的语义选择器（实测命中）---
    'img[aria-label="二维码"]',
    '#animate_qrcode_container img',
    '#douyin_login_comp_scan_code img',
    '[id*="qrcode"] img',
    '[id*="scan_code"] img',
    # 内联 base64 的图片。二维码几乎都是这种形式，但不是所有 base64 图都是二维码，
    # 所以它排在语义选择器之后 —— 靠形状校验兜住误伤。
    'img[src^="data:image/png;base64"]',
    # --- 旧版 / 备用结构 ---
    '[data-e2e="qrcode"]',
    '[class*="qrcode"]',
    '[class*="qr-code"]',
    '[class*="qrCode"]',
    'canvas[class*="qr"]',
    'img[src*="qrcode"]',
    'img[alt*="二维码"]',
    # --- 最后兜底：误伤概率最高，靠形状校验兜住 ---
    '[class*="login"] canvas',
    '[class*="login"] img',
)

# 登录成功的判据：这些元素出现说明已经进到站内了
LOGGED_IN_CANDIDATES: tuple[str, ...] = (
    '[data-e2e="user-info"]',
    '[class*="userInfo"]',
    '[class*="user-info"]',
    '[class*="avatar"]',
    'a[href*="/user/self"]',
    '[class*="conversationList"]',
    '[class*="chatList"]',
)

# 真正的「已登录」标志：这几个 cookie 只在登录成功后才会有。
#
# 为什么需要它们：页面上「看起来登录了」是不可靠的 ——
# 实测踩过的坑：用户只扫码、没在手机上点确认，页面一度把登录弹窗收起来，
# 于是「URL 在站内 + 页面上没有登录标志」成立，被误判为登录成功。
# 结果存下来 28 个 cookie 全是匿名 cookie，一个 sessionid 都没有 ——
# 用户以为登好了，实际上什么都没成。
#
# cookie 是**服务端下发的**，做不了假，所以用它当判据最可靠。
SESSION_COOKIE_NAMES: tuple[str, ...] = (
    "sessionid",
    "sessionid_ss",
    "sid_tt",
    "uid_tt",
)

# 二维码过期 / 需要刷新的标志
QRCODE_EXPIRED_CANDIDATES: tuple[str, ...] = (
    '[class*="expired"]',
    '[class*="refresh"]',
    'text="二维码已失效"',
    'text="刷新"',
    'text="已过期"',
)

# 「已扫描、等你在手机上点确认」的**文案特征**。
#
# 实测（抖音 2026-09）这个状态下登录容器的可见文本是：
#     「需在手机上进行确认 取消登录」
# 此时二维码元素已经被替换掉，页面上只剩这句提示 + 一个「取消登录」。
#
# 为什么要单独识别这个状态：用户在手机上等确认的整段时间里，
# 如果界面一直显示「等待扫码」，用户会以为「我扫了但没反应」，
# 于是反复扫、或者以为卡住了 —— 而实际上球在手机那一边。
SCANNED_TEXT_MARKERS: tuple[str, ...] = (
    "需在手机上进行确认",
    "请在手机上确认",
    "在手机上确认",
    "扫描成功",
)

# 「需要二次验证」的**文案特征**。
#
# 账号在新设备 / 新 IP（比如云服务器）上登录时，抖音经常要求再做一次身份验证——
# 常见的是「选择验证方式」（手机号 / 短信），然后要你填验证码。
#
# ⚠️ 这个页面在**无头浏览器里点不到**：界面上没有任何自动化可以可靠驱动的按钮，
# 而用户在网页工作台里只能看到一个二维码，看不到这个页面 —— 于是会卡死：
# 手机确认了、二维码也没了、但永远登不进去（实测踩过：扫码后保存下来的
# 会话在服务端并不被认可，/chat 仍然显示登录墙）。
#
# 现在识别出这个状态后，前端会展示「登录页操作」面板（实时截图 + 点击 + 输入），
# 让用户在浏览器里亲手把验证走完。
#
# ⚠️ 只放**二次验证页面独有**的文案。像「获取验证码」「验证码登录」在**初始登录页**
# 上也有，放进来会把初始页误判成验证页 —— 那会让「等待扫码」的提示消失。
VERIFY_TEXT_MARKERS: tuple[str, ...] = (
    "选择验证方式",
    "请选择验证方式",
    "换个方式验证",
    "使用其他方式验证",
    "安全验证",
    "身份验证",
    "验证身份",
    "请完成验证",
    "短信验证",
    "手机号验证",
    "完成验证",
)

# 二次验证页面常见的容器 / 元素标识（命中任意一个即认为是验证页）
VERIFY_ELEMENT_CANDIDATES: tuple[str, ...] = (
    '[data-e2e*="verify"]',
    '[class*="verify"]',
    '[class*="Verify"]',
    '[id*="verify"]',
    '[class*="second-verify"]',
    '[class*="SecondVerify"]',
)

# 语义选择器：只有真正的二维码才会命中它。
#
# 关键区别：宽松兜底选择器（``[class*="login"] img`` 之类）在「已扫描」状态下
# **仍然会命中**（命中的是被替换进来的示意图），所以**不能**用它判断
# 「还在等扫码」—— 那会一直误判成 awaiting_scan，用户永远看不到真实进度。
_SEMANTIC_QRCODE_SELECTOR = 'img[aria-label="二维码"]'

# 登录容器：用来读它内部的提示文案
_LOGIN_CONTAINER_SELECTORS: tuple[str, ...] = (
    "#douyin_login_comp_scan_code",
    '[id*="scan_code"]',
    '[id*="douyin_login"]',
)

# 公开别名：工作台的「登录页操作」中继也要用它来**限定查找范围**
# （找输入框时先在这个容器里找，避免误点页面上的其他输入框）。
LOGIN_CONTAINER_SELECTORS: tuple[str, ...] = _LOGIN_CONTAINER_SELECTORS


class LoginState(str, Enum):
    """登录流程的状态。"""

    INITIALIZING = "initializing"   # 正在打开页面
    AWAITING_SCAN = "awaiting_scan"  # 二维码已展示，等你扫
    SCANNED = "scanned"              # 扫了但还没确认
    VERIFYING = "verifying"          # 需要二次验证（选验证方式 / 填短信验证码）
    SUCCESS = "success"              # 登录成功
    EXPIRED = "expired"              # 二维码过期，需要刷新
    FAILED = "failed"                # 出错


@dataclass(frozen=True, slots=True)
class QrCode:
    """一张二维码图片。"""

    image_base64: str
    mime: str = "image/png"
    expires_hint_seconds: int = 120
    detail: str = ""

    @property
    def data_url(self) -> str:
        """直接能塞进 ``<img src="">`` 的形式。"""
        return f"data:{self.mime};base64,{self.image_base64}"


@dataclass(frozen=True, slots=True)
class LoginPoll:
    """一次轮询的结果。"""

    state: LoginState
    detail: str = ""


class QrCodeUnavailableError(RuntimeError):
    """二维码截不出来。"""


def fetch_qrcode(
    page: Any,
    *,
    timeout_ms: int = 20_000,
    navigate: bool = True,
) -> QrCode:
    """打开登录页并截取二维码。

    ``navigate=False`` 用于「页面上二维码已经在了，只是要重新取一次图」
    的场景（比如前端点「刷新二维码」）。
    """
    if navigate:
        try:
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:
            raise QrCodeUnavailableError(
                f"打开登录页失败：{type(exc).__name__}: {exc}\n"
                "如果服务器无法访问抖音，检查网络和 DNS。"
            ) from exc

    element = _locate_qrcode(page, timeout_ms=timeout_ms)
    if element is None:
        raise QrCodeUnavailableError(
            "在登录页上找不到二维码。\n"
            f"试过的选择器：{sel.describe_candidates(QRCODE_CANDIDATES, limit=5)}\n"
            "可能原因：页面还没加载完、抖音改了登录页结构、"
            "或者当前已经处于登录状态（那就直接可用，不需要扫码）。"
        )

    try:
        # 等它可见，避免截到一张透明的空图
        element.wait_for(state="visible", timeout=timeout_ms)
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("等待二维码可见超时：%s", exc)

    try:
        png_bytes = element.screenshot(type="png")
    except Exception as exc:
        raise QrCodeUnavailableError(
            f"截取二维码失败：{type(exc).__name__}: {exc}"
        ) from exc

    if not png_bytes:
        raise QrCodeUnavailableError("截取二维码得到了空图片")

    _assert_capture_is_plausible(png_bytes, element)

    return QrCode(
        image_base64=base64.b64encode(png_bytes).decode("ascii"),
        detail="用手机抖音 App 扫码",
    )


def poll_login(
    page: Any,
    *,
    timeout_ms: int = 3_000,
) -> LoginPoll:
    """检查一次登录进度。

    单次调用很快（不阻塞），由前端按秒轮询。
    这样用户关掉页面也不会留下后台在跑的循环。

    **判定顺序很重要**，每一步的取舍见下面注释。

    ⚠️ 登录成功的判据在 2026-09 被**收紧**过，原因是一次真实的静默失败：

        只扫码、手机上也确认了，但抖音要求再做一次「二次验证」。
        无头浏览器没法完成它，可 ``poll_login`` 当时只看「上下文里有没有
        名叫 sessionid 的 cookie」——而抖音在**尚未真正授权**的阶段就会下发
        这些名字的 cookie，于是接口报「登录成功」、把这份无效会话存了下来。
        结果：工作台显示已绑定，实际 /chat 永远是登录墙，
        所有同步/发送全部报「登录态已失效」，而且**没有任何地方报错**。

    所以现在的成功判据是**两个正向证据同时成立**：

    1. 上下文里真的拿到了会话 cookie（服务端下发，做不了假）；
    2. **会话页结构真的渲染出来了**（会话行 / 搜索框 / 输入框），
       并且登录界面已经消失。

    第 2 条是关键补强：光有 cookie 不够 —— 必须证明「这个会话真的能用」。
    """
    # 1. 真正的成功：会话 cookie + 会话页结构 + 登录界面消失。
    if _has_session_cookie(page) and _chat_page_ready(page):
        return LoginPoll(
            state=LoginState.SUCCESS,
            detail="已登录（检测到会话 cookie，且会话页已正常打开）",
        )

    qr_present = _semantic_qrcode_present(page)

    # 2. 二维码还在 → 直接就是「等待扫码」。
    #
    #    ⚠️ 这一步必须排在**二次验证检测之前**。后者要读登录容器（乃至整页）
    #    的文本，而「等待扫码」通常要持续几十秒、前端每 1.5 秒轮询一次 ——
    #    把它放在前面意味着白白跑几十次文本提取，浏览器线程一直被占着，
    #    用户体感就是「这个界面很容易卡住」。二维码还在时根本没有二次验证这回事。
    #
    #    必须用**语义选择器**判断，不能用 ``_locate_qrcode`` ——
    #    后者带一堆宽松兜底，在「已扫描」状态下仍会命中被替换进去的示意图，
    #    于是永远报「等待扫码」。（这个坑实测踩过。）
    if qr_present:
        return LoginPoll(state=LoginState.AWAITING_SCAN, detail="等待扫码")

    # 3. 已扫描、等你在手机上点确认？
    #
    #    实测该状态的特征是：二维码元素没了，登录容器里显示
    #    「需在手机上进行确认 取消登录」。识别出它，前端才能给出
    #    「请去手机上点确认」这句关键提示 —— 否则用户会以为程序没反应。
    if _login_text_has_marker(page):
        return LoginPoll(
            state=LoginState.SCANNED,
            detail="已扫描，请在手机上点「确认登录」",
        )

    # 4. 需要二次验证？（选验证方式 / 填短信验证码）
    #
    #    这个页面在无头环境里点不到 —— 必须让用户接管。
    #    返回 VERIFYING 后前端会亮出「登录页操作」面板，用户可以实时看到
    #    这一页并亲手点选、填验证码。
    if _verification_prompt_present(page):
        return LoginPoll(
            state=LoginState.VERIFYING,
            detail=(
                "抖音要求二次验证（选择验证方式 / 填短信验证码）。"
                "请在下方「登录页操作」里直接点选、输入验证码。"
            ),
        )

    # 5. 二维码过期了？
    if sel.any_present(page, QRCODE_EXPIRED_CANDIDATES):
        return LoginPoll(state=LoginState.EXPIRED, detail="二维码已失效，请刷新")

    # 6. 其余一律视为「还没登进去」。
    #
    #    这里**刻意删掉了原来那条「URL 在站内就算成功」的判据**。
    #    它是个假阳性制造机：/chat 在未登录时也是这个 URL（页面上弹登录窗），
    #    所以「在站内」证明不了任何事。
    if _login_ui_present(page):
        return LoginPoll(
            state=LoginState.AWAITING_SCAN,
            detail=(
                "页面仍停在登录界面。如果你已经在手机上扫码并确认过，"
                "请以「登录页实时画面」为准 —— 那上面就是抖音此刻真正的页面。"
            ),
        )

    if _has_session_cookie(page):
        # 有 cookie、没有登录界面，但会话页还没渲染出来 —— 再等几轮。
        # 注意：这里**不能**直接判成功。实测存在「有 sessionid 却仍是登录墙」
        # 的状态（扫码了但没真正授权），必须等会话页结构出来才能确认。
        return LoginPoll(
            state=LoginState.INITIALIZING,
            detail=(
                "已拿到登录凭据，正在等待会话页加载…"
                "如果一直停在这里，点一下「刷新页面」；仍不行就重新扫码。"
            ),
        )

    return LoginPoll(state=LoginState.AWAITING_SCAN, detail="等待扫码（二维码未检测到）")


def _login_ui_present(page: Any) -> bool:
    """页面上是否还看得见登录界面。

    这是「没登录成功」的**否定证据**：只要登录界面还在，就绝不能说已登录 ——
    哪怕上下文里已经有 sessionid（这正是那次静默失败的根因）。
    """
    return sel.any_present(page, sel.LOGIN_PAGE_MARKERS)


def _chat_page_ready(page: Any) -> bool:
    """当前页面是否已经是一个**可用的会话页**。

    这是登录成功所需的**正向证据**：会话行 / 搜索框 / 输入框至少出现一个，
    且登录界面已经消失。

    为什么不能只看 cookie：cookie 只能证明「服务端发过」，不能证明
    「这个浏览器现在真的进得去」——实测存在「有 sessionid 但仍是登录墙」的状态。
    """
    if _login_ui_present(page):
        return False
    return sel.any_present(page, sel.CHAT_PAGE_MARKERS)


def _verification_prompt_present(page: Any) -> bool:
    """是否出现了**二次验证**页面。

    判据（按成本从低到高，命中即返回）：

    1. 登录容器里的文案 —— 二次验证就是在登录容器里换内容，最准也最便宜；
    2. 验证页特有容器里的文案（``VERIFY_ELEMENT_CANDIDATES``）。

    刻意只用**独有**文案 —— 「获取验证码」「验证码登录」在初始登录页也有，
    用它们会把初始页误判成验证页。

    ⚠️ 这里**刻意不读整页 body 文本**。``inner_text("body")`` 会强制整页重排，
    在抖音这种页面上要几百毫秒；而它以前是在**每次轮询**（1.5 秒一次）里都跑，
    白白把浏览器线程占住 —— 用户体感就是「这个界面很容易卡住」。
    容器文案已能覆盖真实场景，剩下那点不确定性交给「登录页操作」面板里
    用户的肉眼（面板本来就会把登录页显示出来）。
    """
    for selector in (*_LOGIN_CONTAINER_SELECTORS, *VERIFY_ELEMENT_CANDIDATES):
        try:
            locator = page.locator(selector).first
            if locator.count() <= 0:
                continue
            text = locator.inner_text(timeout=1_000) or ""
        except Exception:  # noqa: BLE001 —— 单个候选失配换下一个
            continue
        if any(marker in text for marker in VERIFY_TEXT_MARKERS):
            return True

    return False


def verify_state_works(browser: Any, state_path: Any, *, timeout_ms: int = 45_000) -> tuple[bool, str]:
    """用**刚保存下来的登录态**开一个干净的上下文，验证它真的能进会话页。

    为什么必须做这一步：``save_storage_state`` 只能保证「文件里有会话 cookie」，
    而那并不等于「这份会话能被服务端认可」。实测发生过：扫码后保存成功，
    但用这份登录态打开 /chat 仍然是登录墙 —— 如果此时宣布「登录成功」，
    用户会以为绑定好了，然后所有发送静默失败。

    所以保存之后必须**真跑一遍**：新建上下文 → 载入登录态 → 打开 /chat →
    确认会话页结构出现。只有通过了才算登录成功。

    返回 ``(是否可用, 说明)``。任何异常都按「不可用」处理并给出原因。
    """
    from .browser import _context_kwargs

    context = None
    try:
        context = browser.new_context(
            **{**_context_kwargs(), "storage_state": str(state_path)}
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"用保存的登录态创建浏览器上下文失败：{type(exc).__name__}: {exc}"

    try:
        page = context.new_page()
        try:
            page.goto(
                LOGIN_URL, wait_until="domcontentloaded", timeout=max(timeout_ms, 15_000)
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"用登录态打开会话页失败：{type(exc).__name__}: {exc}"

        deadline = time.monotonic() + max(5.0, min(timeout_ms, 30_000) / 1000)
        while time.monotonic() < deadline:
            if _chat_page_ready(page):
                return True, "登录态已确认可用（会话页可以正常打开）"
            if _verification_prompt_present(page):
                return False, "打开会话页后仍要求二次验证，登录尚未真正完成"
            # 0.8s 一次：判定本身要打好几个 CDP 往返，太密只是白烧 CPU
            time.sleep(0.8)

        return False, (
            "用保存下来的登录态打开会话页后，看到的仍然是登录界面（或认不出会话页）。"
            "说明这次登录并没有真正生效 —— 常见原因是手机上只扫了码但没点确认，"
            "或者抖音要求了二次验证而验证没有完成。"
        )
    finally:
        if context is not None:
            with contextlib.suppress(Exception):
                context.close()


def _semantic_qrcode_present(page: Any) -> bool:
    """二维码是否真的还在页面上（用语义选择器，不用宽松兜底）。"""
    try:
        return page.locator(_SEMANTIC_QRCODE_SELECTOR).count() > 0
    except Exception:  # noqa: BLE001
        return True


def _login_text_has_marker(page: Any) -> bool:
    """登录容器的可见文本里是否出现「需在手机上进行确认」这类提示。

    拿不到文本时返回 ``False``（宁可退回 AWAITING_SCAN，也不要凭空报 SCANNED）。
    """
    for selector in _LOGIN_CONTAINER_SELECTORS:
        try:
            locator = page.locator(selector).first
            if locator.count() <= 0:
                continue
            text = locator.inner_text(timeout=1_000) or ""
        except Exception:  # noqa: BLE001
            continue
        if any(marker in text for marker in SCANNED_TEXT_MARKERS):
            return True
    return False


def _has_session_cookie(page: Any) -> bool:
    """页面所在上下文里是否已经有登录会话 cookie。

    这是「是否真的登录成功」最可靠的判据：cookie 由服务端下发。

    读不到 cookie（浏览器已关、上下文失效）时返回 ``False`` ——
    宁可多等一轮，也不要凭空宣布登录成功。
    """
    try:
        cookies = page.context.cookies()
    except Exception:  # noqa: BLE001
        return False

    for cookie in cookies or ():
        try:
            if cookie.get("name") in SESSION_COOKIE_NAMES and cookie.get("value"):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def save_storage_state(page: Any, path: Path) -> None:
    """把当前登录态存下来。

    用 ``context.storage_state()`` 拿到 cookie + localStorage，
    然后交给 store 层做原子写 + 权限收紧。

    **不要**在这里直接 ``open().write()`` —— 登录态写坏了就得重新扫码，
    而这正是原子写要防的事。
    """
    import os

    from ..store.atomic import atomic_write_json

    path = Path(path)
    context = page.context

    try:
        state = context.storage_state()
    except Exception as exc:
        raise RuntimeError(f"读取登录态失败：{type(exc).__name__}: {exc}") from exc

    if not state or not state.get("cookies"):
        raise RuntimeError(
            "登录态里没有 cookie —— 可能登录还没真正完成。\n"
            "请确认手机上已经点了「确认登录」，且电脑这边页面已经进入站内。"
        )

    # 光「有 cookie」不够 —— 未登录的页面同样会有一堆匿名 cookie
    #（ttwid / odin_tt / passport_csrf_token 之类），实测存下来 28 个，
    # 一个 sessionid 都没有。必须要求**会话 cookie** 存在。
    cookie_names = {c.get("name") for c in state.get("cookies") or () if isinstance(c, dict)}
    if not (cookie_names & set(SESSION_COOKIE_NAMES)):
        raise RuntimeError(
            "登录态里没有任何会话 cookie（sessionid / sessionid_ss / sid_tt / uid_tt），"
            f"说明这次登录并没有真正完成。当前拿到的 cookie：{sorted(n for n in cookie_names if n)}\n"
            "最常见的原因：**只扫了码，但没有在手机上点「确认登录」**。\n"
            "请在手机上完成确认后重试。"
        )

    atomic_write_json(path, state)

    # 权限收紧：这个文件等同密码
    if os.name != "nt":
        with contextlib.suppress(OSError):
            path.chmod(0o600)

    LOGGER.info("登录态已保存：%s（%d 个 cookie）", path.name, len(state.get("cookies") or []))




# =============================================================================
# 内部
# =============================================================================


# 截出来的 PNG 小于这个字节数就认为不正常。
#
# 一张 178x178 的真二维码 PNG 大约 15 KB；而纯色 / 空白的图会被 PNG 压缩到
# 几百字节。这个阈值用来兜住「截到了一张纯白占位图」——
# 形状是正方形、大小也正常，但里面什么都没有。
_QR_MIN_PNG_BYTES = 1024


def _png_size(png_bytes: bytes) -> tuple[int, int] | None:
    """从 PNG 字节里读出宽高，不依赖任何图像库。

    PNG 的结构：8 字节签名，紧跟着第一个 chunk 一定是 IHDR，
    其数据前 8 个字节就是宽和高（各 4 字节，大端）。

    解析失败返回 ``None``，由调用方决定怎么处理。
    """
    if len(png_bytes) < 24 or png_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    if png_bytes[12:16] != b"IHDR":
        return None

    width = int.from_bytes(png_bytes[16:20], "big")
    height = int.from_bytes(png_bytes[20:24], "big")
    return width, height


def _assert_capture_is_plausible(png_bytes: bytes, element: Any) -> None:
    """第二道防线：校验截出来的 PNG 本身是不是一张像样的二维码图。

    第一道防线（``_locate_qrcode`` 的元素形状校验）挡的是「选到了错误的元素」；
    这一道挡的是「元素选对了，但截出来的是空图 / 加载中的占位」——
    首页的登录弹窗就是这个情况：二维码区是个正方形，形状校验会放行，
    但里面还停在加载态。

    两道合起来的目标只有一个：**宁可报错让你重扫，也绝不把一张不是二维码的
    图当成成功返回**。因为后者会让人对着错误的图反复扫，且不报任何错。
    """
    if len(png_bytes) < _QR_MIN_PNG_BYTES:
        raise QrCodeUnavailableError(
            f"截取到的图片只有 {len(png_bytes)} 字节，太小了，多半是一张空白占位图。\n"
            "这通常意味着登录页的二维码还没加载出来。\n"
            "请稍等几秒后刷新二维码重试；如果一直这样，"
            "检查服务器能否正常访问抖音（网络 / DNS）。"
        )

    size = _png_size(png_bytes)
    if size is None:
        # 不是 PNG 或结构异常 —— 说明拿到的不是我们预期的图
        raise QrCodeUnavailableError(
            "截取到的图片不是有效的 PNG，无法确认它是二维码。"
        )

    width, height = size
    if width <= 0 or height <= 0:
        raise QrCodeUnavailableError(f"截取到的图片尺寸异常：{width}x{height}")

    # 这里的容差比元素校验更宽松 —— 它是兜底，不是主力
    if abs(width / height - 1.0) > _QR_ASPECT_TOLERANCE * 2:
        raise QrCodeUnavailableError(
            f"截取到的图片是 {width}x{height}，不是正方形，几乎可以断定截错了元素"
            "（比如截成了登录弹窗的标题横幅）。\n"
            "这通常说明抖音改了登录页结构，需要更新 "
            "engine/qrlogin.py 里的 QRCODE_CANDIDATES。"
        )


def _locate_qrcode(page: Any, *, timeout_ms: int = 0):
    """定位二维码元素。

    ``timeout_ms > 0`` 时会**反复轮询到截止时间**，这是必须的：

    导航用的是 ``wait_until="domcontentloaded"``，它在登录弹窗渲染出来之前
    就返回了。如果这里只做一次 ``count()`` 检查，几乎必然拿到「找不到」——
    二维码要等前端框架挂载完才出现。
    （这个 bug 真实存在过：单元测试全绿、独立脚本手动 sleep 8 秒也成功，
    唯独走工作台的接口必然失败。）

    另外两条防错措施：

    1. **逐个候选做形状校验**。命中 ≠ 命中了对的东西。宽泛兜底选择器
       (``[class*="login"] img``) 会匹配登录容器的装饰背景横幅，
       截出来的是一张「登录后免费畅享高清视频」的图。后果很恶劣：
       接口返回成功 → 前端把它当二维码显示 → 用户扫它 → 扫不出来，
       而**整个链路不报任何错**。
    2. **命中但形状不对时继续试下一个候选**，而不是就此返回。

    形状判据：二维码必然是接近正方形的，横幅是 8.4:1 那种超宽比例。
    """
    deadline = time.monotonic() + timeout_ms / 1000.0 if timeout_ms > 0 else None

    while True:
        for candidate in QRCODE_CANDIDATES:
            try:
                locator = page.locator(candidate).first
                if locator.count() <= 0:
                    continue
                if _looks_like_qrcode(locator):
                    return locator
                LOGGER.debug(
                    "选择器 %s 命中了元素，但形状不像二维码（多半是装饰图），继续试下一个",
                    candidate,
                )
            except Exception:  # noqa: BLE001
                continue

        if deadline is None or time.monotonic() >= deadline:
            return None

        # 这一轮全都没出现，等一小会儿再来一轮，直到截止时间
        time.sleep(_QR_POLL_INTERVAL_SECONDS)


# 轮询间隔。0.4 秒是个折中：足够快地感知到二维码出现，
# 又不会在 20 秒的等待窗口里把 CPU 打满。
_QR_POLL_INTERVAL_SECONDS = 0.4


# --- 形状校验 -----------------------------------------------------------------
#
# 尺寸区间取得比较宽松，因为不同视口 / 缩放下二维码大小会变。
# 真正起判别作用的是**长宽比** —— 装饰横幅那种超宽比例一抓一个准。
_QR_MIN_SIDE = 32
_QR_MAX_SIDE = 800
_QR_ASPECT_TOLERANCE = 0.20


def _looks_like_qrcode(locator: Any) -> bool:
    """在**形状上**判断一个元素像不像二维码。

    只看几何，不看内容。理由：文本类判别（比如「图里有没有二维码图案」）
    需要图像识别，成本和不确定性都高；而「二维码是正方形」这个特征
    零成本、零依赖，且足够挡住实际遇到的那类误伤。

    拿不到尺寸信息时返回 ``True`` —— 宁可放行，也别因为一次探测失败
    就把真正的二维码排除掉（那是把自己锁在门外）。
    """
    try:
        box = locator.bounding_box()
    except Exception:  # noqa: BLE001
        return True

    if not box:
        # 元素存在但没有布局盒（隐藏 / 未渲染），不是我们要的
        return False

    width = float(box.get("width") or 0)
    height = float(box.get("height") or 0)

    if width <= 0 or height <= 0:
        return False

    if not (_QR_MIN_SIDE <= width <= _QR_MAX_SIDE):
        return False
    if not (_QR_MIN_SIDE <= height <= _QR_MAX_SIDE):
        return False

    return abs(width / height - 1.0) <= _QR_ASPECT_TOLERANCE
