"""扫码登录模块测试。

这个文件里的很多用例，对应的是**真实踩过的坑**，不是想象出来的边界：

1. 二维码定位到的其实是登录弹窗的装饰横幅（接口报成功，给的是横幅图）
2. `_locate_qrcode` 的 timeout 参数不生效，登录弹窗还没渲染就检查，必然找不到
3. 「只扫码、没在手机上确认」被误判成登录成功，存下 28 个匿名 cookie

所以这里的测试目的很明确：**让「静默的错误结果」不可能再发生**。
宁可测出「报错」也不要测出「返回了一个看起来成功但其实是错的东西」。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from douyin_huohua_keeper.engine import qrlogin
from douyin_huohua_keeper.engine.qrlogin import (
    QRCODE_CANDIDATES,
    SESSION_COOKIE_NAMES,
    LoginState,
    QrCodeUnavailableError,
    _assert_capture_is_plausible,
    _has_session_cookie,
    _locate_qrcode,
    _looks_like_qrcode,
    _png_size,
    poll_login,
    save_storage_state,
)

# =============================================================================
# 测试替身
# =============================================================================


class FakeLocator:
    """够用的 locator 替身。"""

    def __init__(
        self,
        hits: int = 0,
        box: dict[str, float] | None = None,
        text: str = "",
    ) -> None:
        self._hits = hits
        self._box = box
        self._text = text
        self.waited = False

    def count(self) -> int:
        return self._hits

    def bounding_box(self) -> dict[str, float] | None:
        return self._box

    def inner_text(self, **kwargs: Any) -> str:
        return self._text

    def wait_for(self, **kwargs: Any) -> None:
        self.waited = True

    @property
    def first(self) -> FakeLocator:
        return self

    def screenshot(self, **kwargs: Any) -> bytes:
        return b"\x89PNG\r\n\x1a\n" + b"\x00" * 4000


class FakeContext:
    def __init__(self, cookies: list[dict[str, Any]] | None = None) -> None:
        self._cookies = cookies if cookies is not None else []

    def cookies(self) -> list[dict[str, Any]]:
        return self._cookies

    def storage_state(self) -> dict[str, Any]:
        return {"cookies": self._cookies, "origins": []}


class FakePage:
    """按「选择器 -> 命中数」映射来响应 locator() 的页面替身。

    ``texts`` 用来模拟 ``inner_text()``（读登录容器里的提示文案）。
    """

    def __init__(
        self,
        hits: dict[str, tuple[int, dict[str, float] | None]] | None = None,
        cookies: list[dict[str, Any]] | None = None,
        url: str = "https://www.douyin.com/chat",
        texts: dict[str, str] | None = None,
    ) -> None:
        self._hits = hits or {}
        self._texts = texts or {}
        self._context = FakeContext(cookies)
        self.url = url
        self.locator_calls: list[str] = []

    def locator(self, selector: str) -> FakeLocator:
        self.locator_calls.append(selector)
        count, box = self._hits.get(selector, (0, None))
        return FakeLocator(count, box, self._texts.get(selector, ""))

    @property
    def context(self) -> FakeContext:
        return self._context


# 一个真实二维码的尺寸：178x178 正方形
SQUARE_BOX = {"width": 178.0, "height": 178.0}
# 登录弹窗里那张装饰横幅：726x86 超宽比例
BANNER_BOX = {"width": 726.0, "height": 86.0}

# 会话 cookie 的样本（值不重要，只测「有没有」）
SESSION_COOKIES = [{"name": "sessionid", "value": "x", "domain": ".douyin.com"}]
# 未登录时页面上的匿名 cookie（实测存下来 28 个这样的）
ANON_COOKIES = [
    {"name": "ttwid", "value": "x", "domain": ".douyin.com"},
    {"name": "odin_tt", "value": "x", "domain": ".douyin.com"},
    {"name": "passport_csrf_token", "value": "x", "domain": ".douyin.com"},
]


# =============================================================================
# 坑 1：定位到的不是二维码，而是装饰横幅
# =============================================================================


class TestLocateQrcodeShapeGuard:
    def test_prefers_semantic_selector(self) -> None:
        """语义选择器命中正方形元素时应直接返回。"""
        page = FakePage({QRCODE_CANDIDATES[0]: (1, SQUARE_BOX)})

        found = _locate_qrcode(page, timeout_ms=0)

        assert found is not None

    def test_skips_banner_and_falls_through_to_real_qrcode(self) -> None:
        """命中超宽横幅时必须跳过，继续试到真正的二维码。

        这是核心回归：以前的实现会直接返回 `.first`，
        结果把「登录后免费畅享高清视频」那张横幅当成二维码返回。
        """
        banner_selector = QRCODE_CANDIDATES[-1]  # '[class*="login"] img'
        real_selector = QRCODE_CANDIDATES[0]     # 'img[aria-label="二维码"]'
        page = FakePage(
            {
                banner_selector: (2, BANNER_BOX),
                real_selector: (1, SQUARE_BOX),
            }
        )

        found = _locate_qrcode(page, timeout_ms=0)

        assert found is not None
        # 必须真的去试了语义选择器
        assert real_selector in page.locator_calls

    def test_returns_none_when_only_banner_present(self) -> None:
        """页面上只有横幅时，应返回 None（报错）而不是把横幅当二维码。"""
        banner_selector = QRCODE_CANDIDATES[-1]
        page = FakePage({banner_selector: (2, BANNER_BOX)})

        assert _locate_qrcode(page, timeout_ms=0) is None

    def test_rejects_landscape_image(self) -> None:
        assert _looks_like_qrcode(FakeLocator(1, BANNER_BOX)) is False

    def test_rejects_hidden_element_without_box(self) -> None:
        """元素存在但没有布局盒（隐藏）—— 不能算命中。"""
        assert _looks_like_qrcode(FakeLocator(1, None)) is False

    def test_accepts_square_element(self) -> None:
        assert _looks_like_qrcode(FakeLocator(1, SQUARE_BOX)) is True

    def test_allows_when_geometry_unavailable(self) -> None:
        """拿不到尺寸时放行 —— 宁可放行也不能把真二维码排除掉。"""

        class Broken:
            def bounding_box(self) -> dict[str, float]:
                raise RuntimeError("no geometry")

        assert _looks_like_qrcode(Broken()) is True

    def test_stable_selectors_are_present(self) -> None:
        """实测确认有效的稳定选择器必须在候选表里，且排在兜底项之前。"""
        assert 'img[aria-label="二维码"]' in QRCODE_CANDIDATES
        assert "#animate_qrcode_container img" in QRCODE_CANDIDATES
        assert "#douyin_login_comp_scan_code img" in QRCODE_CANDIDATES

        # 语义选择器必须在宽泛兜底之前
        semantic = QRCODE_CANDIDATES.index('img[aria-label="二维码"]')
        fallback = len(QRCODE_CANDIDATES) - 1
        assert semantic < fallback


# =============================================================================
# 坑 2：timeout 不生效，登录弹窗还没渲染就放弃
# =============================================================================


class TestLocateQrcodeWaits:
    def test_polls_until_deadline(self) -> None:
        """元素一开始不存在，稍后才出现 —— 必须轮询到它出现。

        回归背景：导航用 domcontentloaded，早于弹窗渲染。
        以前只做一次即时检查，必然报「找不到二维码」。
        """
        state = {"calls": 0}

        class AppearingLocator:
            def __init__(self, page: AppearingPage) -> None:
                self._page = page

            def count(self) -> int:
                state["calls"] += 1
                # 第 4 次检查（约 1.2 秒后）才出现
                return 1 if state["calls"] >= 4 else 0

            def bounding_box(self) -> dict[str, float]:
                return SQUARE_BOX

            @property
            def first(self) -> AppearingLocator:
                return self

        class AppearingPage:
            url = "https://www.douyin.com/chat"

            def locator(self, selector: str) -> AppearingLocator:
                return AppearingLocator(self)

        found = _locate_qrcode(AppearingPage(), timeout_ms=5_000)

        assert found is not None
        assert state["calls"] >= 4

    def test_gives_up_after_deadline(self) -> None:
        """一直没有出现时，到点就返回 None，不能无限等。"""
        page = FakePage({})

        assert _locate_qrcode(page, timeout_ms=600) is None

    def test_instant_check_when_timeout_zero(self) -> None:
        """timeout_ms=0 表示只做一次即时检查（供判定页面类型用）。"""
        page = FakePage({QRCODE_CANDIDATES[0]: (1, SQUARE_BOX)})

        assert _locate_qrcode(page, timeout_ms=0) is not None


# =============================================================================
# PNG 层校验（第二道防线）
# =============================================================================


def make_png(width: int, height: int) -> bytes:
    """手工拼一个最小 PNG 头（只有签名 + IHDR），够 _png_size 解析。"""
    ihdr = b"IHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x06\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + len(ihdr).to_bytes(4, "big") + ihdr


class TestPngSize:
    def test_parses_dimensions(self) -> None:
        assert _png_size(make_png(178, 178)) == (178, 178)

    def test_returns_none_for_non_png(self) -> None:
        assert _png_size(b"not a png at all, definitely not") is None

    def test_returns_none_for_truncated(self) -> None:
        assert _png_size(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4) is None


class TestCapturePlausibility:
    def test_accepts_realistic_qrcode_capture(self) -> None:
        # 真实二维码 PNG 约 7.7 KB
        _assert_capture_is_plausible(make_png(178, 178) + b"\x00" * 8000, None)

    def test_rejects_tiny_image(self) -> None:
        """空白占位图会被 PNG 压到几百字节 —— 必须拒绝。"""
        with pytest.raises(QrCodeUnavailableError, match="太小"):
            _assert_capture_is_plausible(make_png(178, 178) + b"\x00" * 10, None)

    def test_rejects_non_square_capture(self) -> None:
        """截成超宽图 = 截错了元素，必须报错而不是当成成功。"""
        with pytest.raises(QrCodeUnavailableError, match="不是正方形"):
            _assert_capture_is_plausible(make_png(726, 86) + b"\x00" * 8000, None)

    def test_rejects_invalid_png(self) -> None:
        with pytest.raises(QrCodeUnavailableError, match="不是有效的 PNG"):
            _assert_capture_is_plausible(b"x" * 5000, None)


# =============================================================================
# 坑 3：只扫码未确认，被误判成登录成功
# =============================================================================


class TestSessionCookieDetection:
    def test_detects_session_cookie(self) -> None:
        page = FakePage(cookies=SESSION_COOKIES)
        assert _has_session_cookie(page) is True

    def test_anonymous_cookies_are_not_enough(self) -> None:
        """ttwid / odin_tt 这类匿名 cookie 遍地都是，不能当作已登录。"""
        page = FakePage(cookies=ANON_COOKIES)
        assert _has_session_cookie(page) is False

    def test_empty_value_is_not_a_session(self) -> None:
        page = FakePage(cookies=[{"name": "sessionid", "value": ""}])
        assert _has_session_cookie(page) is False

    def test_broken_context_returns_false(self) -> None:
        class BrokenPage:
            url = ""

            @property
            def context(self) -> Any:
                raise RuntimeError("context closed")

        assert _has_session_cookie(BrokenPage()) is False

    def test_all_known_session_cookie_names_accepted(self) -> None:
        for name in SESSION_COOKIE_NAMES:
            page = FakePage(cookies=[{"name": name, "value": "v"}])
            assert _has_session_cookie(page) is True, name


class TestPollLoginSuccessDetection:
    def test_session_cookie_alone_is_not_success(self) -> None:
        """核心回归：**光有会话 cookie 不算登录成功。**

        实测事故：抖音会在「尚未真正授权」的阶段就下发名为 ``sessionid`` 的
        cookie，于是只判断 «cookie 存在» 会把「扫码了但没登进去」误报成成功，
        存下一份服务端并不认可的登录态 —— 工作台显示已绑定，
        实际 /chat 永远是登录墙，之后所有同步/发送全部报「登录态已失效」，
        而且没有任何地方报错。

        所以成功必须再叠一条**正向证据**：会话页结构真的渲染出来了。
        """
        page = FakePage(cookies=SESSION_COOKIES)

        poll = poll_login(page, timeout_ms=0)

        assert poll.state is not LoginState.SUCCESS

    def test_reports_success_when_cookie_and_chat_page(self) -> None:
        """会话 cookie + 会话页结构 → 才是真的成功。"""
        page = FakePage(
            hits={'[data-e2e="conversation-item"]': (5, SQUARE_BOX)},
            cookies=SESSION_COOKIES,
        )

        poll = poll_login(page, timeout_ms=0)

        assert poll.state is LoginState.SUCCESS

    def test_does_not_report_success_on_chat_url_without_session(self) -> None:
        """核心回归：停在 /chat + 页面上有二维码 + 只有匿名 cookie → 绝不能报成功。

        这正是「只扫码未确认」时真实发生的情形：
        以前靠「URL 在站内」就宣布成功，存下 28 个匿名 cookie 骗了用户。
        """
        page = FakePage(
            hits={QRCODE_CANDIDATES[0]: (1, SQUARE_BOX)},
            cookies=ANON_COOKIES,
            url="https://www.douyin.com/chat",
        )

        poll = poll_login(page, timeout_ms=0)

        assert poll.state is not LoginState.SUCCESS
        assert poll.state is LoginState.AWAITING_SCAN

    def test_does_not_report_success_when_qrcode_still_visible(self) -> None:
        """页面上还有二维码 = 铁定没登进去，即使命中了「已登录」元素也不行。

        登录页上存在头像类元素（推荐流作者头像）是会发生的，
        所以元素判据必须叠加「二维码已消失」。
        """
        page = FakePage(
            hits={
                QRCODE_CANDIDATES[0]: (1, SQUARE_BOX),
                '[class*="avatar"]': (3, SQUARE_BOX),
            },
            cookies=ANON_COOKIES,
        )

        poll = poll_login(page, timeout_ms=0)

        assert poll.state is not LoginState.SUCCESS

    def test_logged_in_elements_without_session_cookie_is_not_success(self) -> None:
        """「页面上有头像类元素」不足以证明登录成功。

        登录页上的推荐流作者头像、广告位头像都可能命中 ``[class*="avatar"]``，
        所以这条曾经是假阳性来源之一；现在必须叠加会话 cookie。
        """
        page = FakePage(
            hits={'[class*="avatar"]': (1, SQUARE_BOX)},
            cookies=ANON_COOKIES,
        )

        poll = poll_login(page, timeout_ms=0)

        assert poll.state is not LoginState.SUCCESS

    def test_reports_expired(self) -> None:
        page = FakePage(
            hits={qrlogin.QRCODE_EXPIRED_CANDIDATES[0]: (1, SQUARE_BOX)},
            cookies=ANON_COOKIES,
        )

        poll = poll_login(page, timeout_ms=0)

        assert poll.state is LoginState.EXPIRED


# =============================================================================
# 登录态保存：拒绝存下无效的登录态
# =============================================================================


class TestPollLoginScannedState:
    """「已扫描、等手机上确认」必须被识别出来。

    回归背景：用户在手机上等确认的整段时间里，旧实现因为宽松兜底选择器
    仍能命中被替换进去的示意图，一直报 `awaiting_scan`，
    界面显示「等待扫码」——用户以为自己扫失败了，反复重扫。
    实测该状态的判据（抖音 2026-09）：
      - 语义选择器 img[aria-label="二维码"] 命中数 = 0
      - 登录容器文本 = 「需在手机上进行确认 取消登录」
    """

    def test_detects_scanned_state(self) -> None:
        page = FakePage(
            hits={
                # 语义选择器不命中（二维码已被替换）
                # 但宽松兜底仍命中——这正是旧实现被误导的原因
                '[class*="login"] img': (2, SQUARE_BOX),
                "#douyin_login_comp_scan_code": (1, SQUARE_BOX),
            },
            cookies=ANON_COOKIES,
            url="https://www.douyin.com/chat",
            texts={"#douyin_login_comp_scan_code": "需在手机上进行确认 取消登录"},
        )

        poll = poll_login(page, timeout_ms=0)

        assert poll.state is LoginState.SCANNED
        assert "确认" in poll.detail

    def test_loose_fallback_must_not_mask_scanned(self) -> None:
        """宽松兜底命中 ≠ 二维码还在。这条是核心回归。"""
        page = FakePage(
            hits={
                QRCODE_CANDIDATES[-1]: (2, SQUARE_BOX),  # '[class*="login"] img'
                "#douyin_login_comp_scan_code": (1, SQUARE_BOX),
            },
            cookies=ANON_COOKIES,
            texts={"#douyin_login_comp_scan_code": "需在手机上进行确认 取消登录"},
        )

        poll = poll_login(page, timeout_ms=0)

        assert poll.state is not LoginState.AWAITING_SCAN

    def test_scanned_takes_precedence_over_success_elements(self) -> None:
        """页面上有头像类元素，但只要还写着「需在手机上确认」，就没登录成功。"""
        page = FakePage(
            hits={
                '[class*="avatar"]': (1, SQUARE_BOX),
                "#douyin_login_comp_scan_code": (1, SQUARE_BOX),
            },
            cookies=ANON_COOKIES,
            texts={"#douyin_login_comp_scan_code": "需在手机上进行确认 取消登录"},
        )

        poll = poll_login(page, timeout_ms=0)

        assert poll.state is LoginState.SCANNED

    def test_qrcode_still_present_is_awaiting_scan(self) -> None:
        """二维码还在时（语义选择器命中）必须是等待扫码。"""
        page = FakePage(
            hits={
                'img[aria-label="二维码"]': (1, SQUARE_BOX),
                "#douyin_login_comp_scan_code": (1, SQUARE_BOX),
            },
            cookies=ANON_COOKIES,
            texts={"#douyin_login_comp_scan_code": "如何扫码 打开「抖音APP」点击左上角 扫一扫"},
        )

        poll = poll_login(page, timeout_ms=0)

        assert poll.state is LoginState.AWAITING_SCAN

    def test_scanned_marker_variants(self) -> None:
        """几种可能的文案变体都能识别（抖音改文案时不至于立刻失效）。"""
        for text in (
            "需在手机上进行确认 取消登录",
            "请在手机上确认",
            "扫描成功，请在手机上确认登录",
        ):
            page = FakePage(
                hits={"#douyin_login_comp_scan_code": (1, SQUARE_BOX)},
                cookies=ANON_COOKIES,
                texts={"#douyin_login_comp_scan_code": text},
            )
            assert poll_login(page, timeout_ms=0).state is LoginState.SCANNED, text


class TestVerificationDetection:
    """「需要二次验证」必须被单独识别出来。

    背景：账号在新设备 / 新 IP（云服务器）上登录时，抖音常要求
    「选择验证方式」→ 手机号 / 短信。这个页面在无头浏览器里点不到，
    用户在网页工作台里也看不到 —— 于是卡在「手机确认了却一直登不进去」。
    识别出它，前端才能亮出「登录页操作」面板让用户亲手完成验证。
    """

    def test_detects_verification_page(self) -> None:
        page = FakePage(
            hits={"#douyin_login_comp_scan_code": (1, SQUARE_BOX)},
            cookies=ANON_COOKIES,
            texts={"#douyin_login_comp_scan_code": "请选择验证方式 手机号验证 短信验证"},
        )

        poll = poll_login(page, timeout_ms=0)

        assert poll.state is LoginState.VERIFYING
        assert "验证" in poll.detail

    def test_initial_login_page_is_not_verification(self) -> None:
        """初始登录页上有「验证码登录 / 密码登录 / 获取验证码」——

        这些字样**不能**被当成二次验证的判据，否则「等待扫码」的提示会消失，
        用户反而不知道自己在等什么。
        """
        page = FakePage(
            hits={
                "#douyin_login_comp_scan_code": (1, SQUARE_BOX),
                'img[aria-label="二维码"]': (1, SQUARE_BOX),
            },
            cookies=ANON_COOKIES,
            texts={
                "#douyin_login_comp_scan_code": (
                    "扫码登录 如何扫码 打开「抖音APP」点击左上角 扫一扫 "
                    "验证码登录 密码登录 获取验证码 登录"
                )
            },
        )

        poll = poll_login(page, timeout_ms=0)

        assert poll.state is LoginState.AWAITING_SCAN


class TestSaveStorageState:
    def test_saves_when_session_cookie_present(self, tmp_path: Path) -> None:
        page = FakePage(cookies=SESSION_COOKIES)
        target = tmp_path / "main.state.json"

        save_storage_state(page, target)

        assert target.exists()

    def test_refuses_anonymous_only_cookies(self, tmp_path: Path) -> None:
        """核心回归：只有匿名 cookie 时必须报错，绝不能存下一个假的登录态。

        后果对比：
        - 报错 → 用户知道要重新确认登录，火花还有救
        - 静默存下 → 用户以为登好了，第二天发送全失败，火花断了才知道
        """
        page = FakePage(cookies=ANON_COOKIES)
        target = tmp_path / "main.state.json"

        with pytest.raises(RuntimeError, match="会话 cookie"):
            save_storage_state(page, target)

        assert not target.exists()

    def test_refuses_no_cookies_at_all(self, tmp_path: Path) -> None:
        page = FakePage(cookies=[])
        target = tmp_path / "main.state.json"

        with pytest.raises(RuntimeError, match="没有 cookie"):
            save_storage_state(page, target)

        assert not target.exists()

    def test_error_message_mentions_phone_confirmation(self, tmp_path: Path) -> None:
        """报错要说人话：直接点出「没在手机上确认登录」这个最常见原因。"""
        page = FakePage(cookies=ANON_COOKIES)

        with pytest.raises(RuntimeError) as excinfo:
            save_storage_state(page, tmp_path / "s.json")

        assert "确认登录" in str(excinfo.value)
