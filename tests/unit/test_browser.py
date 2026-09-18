"""浏览器层：资源拦截器。

为什么值得单独测：这是**唯一会主动丢掉页面请求**的地方 —— 拦错了页面就崩，
而它的收益只是"更快、更省内存"。所以这里要钉死两条：
1. 默认**不拦 image**（二维码万一变成网络图片，拦了就登录不了）；
2. 任何意外都不许把页面搞坏（退化成放行）。
"""

from __future__ import annotations

from typing import Any

import pytest

from douyin_huohua_keeper.engine.browser import (
    _chromium_args,
    _install_resource_blocker,
    _needs_no_sandbox,
)


class FakeRoute:
    def __init__(self) -> None:
        self.actions: list[str] = []

    def abort(self) -> None:
        self.actions.append("abort")

    def continue_(self) -> None:
        self.actions.append("continue")


class FakeRequest:
    def __init__(self, url: str, resource_type: str) -> None:
        self.url = url
        self.resource_type = resource_type


class FakePage:
    """只记录 handler，不真的连浏览器。"""

    def __init__(self) -> None:
        self.routes: list[tuple[str, Any]] = []

    def route(self, pattern: str, handler: Any) -> None:
        self.routes.append((pattern, handler))

    def fire(self, url: str, resource_type: str) -> str:
        assert self.routes, "没有安装拦截器"
        route = FakeRoute()
        self.routes[0][1](route, FakeRequest(url, resource_type))
        assert route.actions, "handler 什么都没做"
        return route.actions[-1]


class TestResourceBlocker:
    def test_blocks_media_and_font(self) -> None:
        page = FakePage()
        _install_resource_blocker(page, ("font", "media"))

        assert page.fire("https://p3.douyinpic.com/aweme/v1/play/", "media") == "abort"
        assert page.fire("https://sf.douyin.com/x.woff2", "font") == "abort"

    @pytest.mark.parametrize(
        ("url", "resource_type"),
        [
            ("https://p3-pc.douyinpic.com/avatar.jpeg", "image"),  # 头像：留着
            ("https://www.douyin.com/chat", "document"),
            ("https://sf.douyin.com/app.js", "script"),
            ("https://www.douyin.com/im/api/msg", "xhr"),
        ],
    )
    def test_keeps_everything_else(self, url: str, resource_type: str) -> None:
        """默认策略是保守的：只动 media/font，其余一律放行。"""
        page = FakePage()
        _install_resource_blocker(page, ("font", "media"))
        assert page.fire(url, resource_type) == "continue"

    def test_never_touches_data_uri(self) -> None:
        """二维码在页面上就是 data URI —— 就算类型命中也不许拦。"""
        page = FakePage()
        _install_resource_blocker(page, ("image", "media", "font"))
        assert page.fire("data:image/png;base64,iVBORw0KGgo=", "image") == "continue"
        assert page.fire("blob:https://www.douyin.com/abc", "media") == "continue"

    def test_empty_tuple_installs_nothing(self) -> None:
        """`HUOHUA_BLOCK_RESOURCE_TYPES=none` → 完全不装拦截器（排障用）。"""
        page = FakePage()
        _install_resource_blocker(page, ())
        assert page.routes == []

    def test_route_errors_degrade_to_continue(self) -> None:
        """任何异常都必须退化成放行 —— 拦截器绝不能把页面搞坏。"""

        class ExplodingRoute(FakeRoute):
            def abort(self) -> None:
                raise RuntimeError("boom")

            def continue_(self) -> None:
                raise RuntimeError("boom too")

        page = FakePage()
        _install_resource_blocker(page, ("media",))
        route = ExplodingRoute()
        # 不该抛出去
        page.routes[0][1](route, FakeRequest("https://x/v.mp4", "media"))

    def test_route_registration_failure_is_not_fatal(self) -> None:
        """连 route() 都失败时，只能记警告、不能抛。"""

        class BrokenPage:
            def route(self, pattern: str, handler: Any) -> None:
                raise RuntimeError("no routing here")

        _install_resource_blocker(BrokenPage(), ("media",))  # 不抛即通过


class TestChromiumSandbox:
    """沙箱只在容器（或 Linux root）里关 —— **桌面不该关**。

    背景：原来 ``--no-sandbox`` 是无条件加的。容器确实需要它（没有 user namespace 权限），
    但桌面上关掉沙箱等于让网页代码以用户权限运行 —— 白白牺牲安全换不到任何东西。
    """

    def test_container_gets_both_flags(self) -> None:
        args = _chromium_args(no_sandbox=True, small_shm=True)
        assert "--no-sandbox" in args
        assert "--disable-dev-shm-usage" in args

    def test_desktop_keeps_sandbox(self) -> None:
        args = _chromium_args(no_sandbox=False, small_shm=False)
        assert "--no-sandbox" not in args
        assert "--disable-dev-shm-usage" not in args
        # 与沙箱无关的参数照旧
        assert "--disable-gpu" in args
        assert "--no-first-run" in args

    def test_root_on_linux_needs_no_sandbox_but_not_shm_hack(self) -> None:
        """Linux 上以 root 跑：Chromium 会拒绝启动，必须关沙箱；但 /dev/shm 是正常的。"""
        args = _chromium_args(no_sandbox=True, small_shm=False)
        assert "--no-sandbox" in args
        assert "--disable-dev-shm-usage" not in args

    @pytest.mark.parametrize(("mode", "expected"), [("always", True), ("never", False)])
    def test_explicit_modes(self, mode: str, expected: bool) -> None:
        assert _needs_no_sandbox(mode) is expected

    def test_auto_follows_container_detection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("douyin_huohua_keeper.engine.browser._in_container", lambda: True)
        assert _needs_no_sandbox("auto") is True

        monkeypatch.setattr("douyin_huohua_keeper.engine.browser._in_container", lambda: False)
        # 非容器、非 Linux-root（本机是 Windows）→ 保留沙箱
        import sys

        if sys.platform.startswith("linux"):
            pytest.skip("Linux 上逻辑还要看 geteuid，本用例只验证非容器分支")
        assert _needs_no_sandbox("auto") is False
