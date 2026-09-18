"""针对「收件人同步」与「设置页（通知测试 / 日志 / 自检）」问题的回归测试。

覆盖的是一批真实踩过的坑：

1. ``from ...notify import AlertLevel`` 导不出来 → 通知测试接口 500。
2. 全项目没有日志落盘配置 → 日志页永远为空，500 也查不到痕迹。
3. 自检只看配置项在不在 → 通知发不出去、日志没落盘，也照样「通过」。
4. ``/api/contacts/sync`` 没装载登录态 → 永远读到空会话列表，什么也同步不进来。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import random
import time
import types
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from douyin_huohua_keeper.config import load_settings
from douyin_huohua_keeper.engine import navigator
from douyin_huohua_keeper.engine.auth import check_cookie_freshness
from douyin_huohua_keeper.models import Contact
from douyin_huohua_keeper.notify import DeliveryResult, DispatchReport
from douyin_huohua_keeper.store.atomic import CorruptFileError, read_json
from douyin_huohua_keeper.store.lock import FileLock, LockBusyError
from douyin_huohua_keeper.store.repo import Repository
from douyin_huohua_keeper.workbench.app import AppState, create_app

TOKEN = "unit-test-token-0123456789abcdef"
HDR = {"X-Huohua-Token": TOKEN}


# =============================================================================
# 替身：页面节点树 + 引擎
# =============================================================================


class Node:
    def __init__(
        self,
        text: str = "",
        children: dict[str, list[Node]] | None = None,
        attrs: dict[str, str] | None = None,
    ) -> None:
        self.text = text
        self.children = children or {}
        self.attrs = attrs or {}


class Loc:
    def __init__(self, nodes: list[Node]) -> None:
        self._nodes = nodes

    def count(self) -> int:
        return len(self._nodes)

    def inner_text(self, **kw: Any) -> str:
        return self._nodes[0].text if self._nodes else ""

    def get_attribute(self, name: str) -> str | None:
        return self._nodes[0].attrs.get(name) if self._nodes else None

    @property
    def first(self) -> Loc:
        return Loc(self._nodes[:1])

    def nth(self, index: int) -> Loc:
        return Loc(self._nodes[index : index + 1])

    def locator(self, selector: str) -> Loc:
        out: list[Node] = []
        for node in self._nodes:
            out.extend(node.children.get(selector, []))
        return Loc(out)


class Page:
    def __init__(
        self,
        children: dict[str, list[Node]] | None = None,
        avatar_bodies: dict[str, bytes] | None = None,
    ) -> None:
        self._children = children or {}
        self.avatar_bodies = avatar_bodies or {}

    def locator(self, selector: str) -> Loc:
        return Loc(self._children.get(selector, []))

    @property
    def request(self) -> Any:
        page = self

        class _Req:
            def get(self, url: str, timeout: float = 15.0) -> Any:
                body = page.avatar_bodies.get(url)
                if body is None:
                    raise RuntimeError("boom")
                return types.SimpleNamespace(
                    body=lambda: body,
                    headers={"content-type": "image/png"},
                )

        return _Req()


class FakeNavEngine:
    """服务 /api/contacts/sync：支持登录态装载、登录校验、会话读取。"""

    def __init__(self, page: Page, *, logged_in: bool = True) -> None:
        self.page = page
        self.logged_in = logged_in
        self.loaded_state: Any = None
        self.session = types.SimpleNamespace(
            call=lambda fn: fn(types.SimpleNamespace(page=page))
        )

    def load_state(self, path: Any) -> None:
        self.loaded_state = path

    def check_login(self) -> Any:
        ok = self.logged_in
        return types.SimpleNamespace(ok=ok, reason="OK" if ok else "AUTH", detail="ok" if ok else "未登录")

    def stop(self) -> None:
        pass


def _row(name: str, streak: str, src: str) -> Node:
    return Node(
        f"{name} {streak} 刚刚",
        children={
            '[class*="ConversationItemtitle"]': [Node(name)],
            '[class*="Streak"]': [Node(streak)],
            '[class*="IMAvataravatarContainer"] img': [Node("", attrs={"src": src})],
        },
    )


def _friend_page() -> Page:
    rows = [
        _row("阿明", "849", "https://cdn.example/a.png"),
        _row("阿豪", "127", "https://cdn.example/b.png"),
        _row("阿糕", "重燃中 2/3", "https://cdn.example/c.png"),
    ]
    return Page(
        children={'[data-e2e="conversation-item"]': rows},
        avatar_bodies={
            "https://cdn.example/a.png": b"AAA",
            "https://cdn.example/b.png": b"BBB",
            "https://cdn.example/c.png": b"CCC",
        },
    )


def _settings(
    tmp_path: Any,
    *,
    token: str = TOKEN,
    notify: Any = None,
    log_dir: Any = None,
) -> Any:
    base = load_settings()
    # allowed_ips 显式清空：测试不该依赖跑测机器上的 .env。否则 TestClient 的
    # host（"testclient"）会被 IP 白名单挡掉，拿到 403 而不是被测的返回值。
    workbench = dataclasses.replace(base.workbench, token=token, allowed_ips=())
    extra: dict[str, Any] = {}
    if notify is not None:
        extra["notify"] = notify
    if log_dir is not None:
        extra["log_dir"] = log_dir
    return dataclasses.replace(base, data_dir=tmp_path / "data", workbench=workbench, **extra)


# =============================================================================
# 1. notify 包导出 AlertLevel（回归：导入失败 → 500）
# =============================================================================


def test_notify_package_exports_alert_level() -> None:
    from douyin_huohua_keeper.models import AlertLevel as FromModels
    from douyin_huohua_keeper.notify import AlertLevel as FromNotify

    assert FromNotify is FromModels


# =============================================================================
# 2. 通知测试接口不再 500
# =============================================================================


class TestNotifyTestEndpoint:
    def test_no_channels_returns_structured_200(self, tmp_path: Any) -> None:
        """没有配置通道时应返回可读的 200，而不是 500。"""
        app = create_app(_settings(tmp_path, notify=dataclasses.replace(load_settings().notify, channels=())))
        client = TestClient(app)

        resp = client.post("/api/system/notify/test", headers=HDR)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is False
        assert "没有配置任何通知通道" in body["detail"]

    def test_failing_channel_reports_failure_not_500(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """通道发送失败要如实汇报，而不是抛 500。"""
        _patch_dispatcher(monkeypatch, ok=False)
        notify = dataclasses.replace(
            load_settings().notify, channels=("webhook",), generic_webhook="http://example.invalid/hook"
        )
        app = create_app(_settings(tmp_path, notify=notify))
        client = TestClient(app)

        resp = client.post("/api/system/notify/test", headers=HDR)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is False
        assert body["failed_channels"] == ["webhook"]
        assert body["results"][0]["detail"]

    def test_successful_channel_reports_ok(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_dispatcher(monkeypatch, ok=True)
        notify = dataclasses.replace(
            load_settings().notify, channels=("webhook",), generic_webhook="http://example.invalid/hook"
        )
        app = create_app(_settings(tmp_path, notify=notify))
        client = TestClient(app)

        body = client.post("/api/system/notify/test", headers=HDR).json()
        assert body["ok"] is True
        assert body["succeeded_channels"] == ["webhook"]


# =============================================================================
# 3. 日志：能落盘 + 接口能读到
# =============================================================================


class TestLogs:
    def test_logs_endpoint_reads_file(self, tmp_path: Any) -> None:
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "huohua.log").write_text("第一行\n第二行\n", encoding="utf-8")

        app = create_app(_settings(tmp_path, log_dir=log_dir))
        client = TestClient(app)
        body = client.get("/api/system/logs", headers=HDR).json()

        assert body["line_count"] == 2
        assert "第一行" in body["lines"]

    def test_configure_logging_writes_and_is_idempotent(self, tmp_path: Any) -> None:
        import douyin_huohua_keeper.logging_setup as ls

        log_dir = tmp_path / "logs"
        settings = types.SimpleNamespace(log_dir=log_dir, log_level="INFO")
        try:
            path = ls.configure_logging(settings, force=True)
            assert path is not None
            assert path.parent.is_dir()

            logging.getLogger("douyin_huohua_keeper.regression").warning("hello-log-line")
            for handler in logging.getLogger().handlers:
                handler.flush()
            assert "hello-log-line" in path.read_text(encoding="utf-8")

            # 幂等：再调一次不换文件、不叠加 handler
            again = ls.configure_logging(settings)
            assert again == path
        finally:
            root = logging.getLogger()
            for handler in list(root.handlers):
                if getattr(handler, "_huohua_file_handler", False):
                    root.removeHandler(handler)
                    handler.close()
            ls._configured_path = None  # 测试收尾，清掉模块级缓存


# =============================================================================
# 4. 自检要能真的发现问题
# =============================================================================


class TestSelfCheck:
    def test_check_fails_when_logs_missing(self, tmp_path: Any) -> None:
        """日志没落盘时，自检必须判不通过。"""
        settings = _settings(
            tmp_path,
            notify=dataclasses.replace(load_settings().notify, channels=()),
            log_dir=tmp_path / "logs",
        )
        app = create_app(settings)
        client = TestClient(app)

        body = client.post("/api/system/check", headers=HDR).json()
        by_name = {c["name"]: c for c in body["checks"]}
        assert by_name["日志记录"]["status"] == "fail"
        assert body["summary"]["healthy"] is False

    def test_check_fails_when_notify_broken(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """通知发不出去时，自检必须判不通过（而不是只看配置项在不在）。"""
        _patch_dispatcher(monkeypatch, ok=False)
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "huohua.log").write_text("x\n", encoding="utf-8")  # 让日志项通过，隔离出通知项

        notify = dataclasses.replace(
            load_settings().notify, channels=("webhook",), generic_webhook="http://example.invalid/hook"
        )
        app = create_app(_settings(tmp_path, notify=notify, log_dir=log_dir))
        client = TestClient(app)

        body = client.post("/api/system/check", headers=HDR).json()
        by_name = {c["name"]: c for c in body["checks"]}
        assert by_name["通知通道"]["status"] == "fail"
        assert body["summary"]["healthy"] is False

    def test_check_passes_when_notify_ok_and_logs_present(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_dispatcher(monkeypatch, ok=True)
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "huohua.log").write_text("x\n", encoding="utf-8")

        notify = dataclasses.replace(
            load_settings().notify, channels=("webhook",), generic_webhook="http://example.invalid/hook"
        )
        app = create_app(_settings(tmp_path, notify=notify, log_dir=log_dir))
        client = TestClient(app)

        body = client.post("/api/system/check", headers=HDR).json()
        by_name = {c["name"]: c for c in body["checks"]}
        assert by_name["通知通道"]["status"] == "ok"
        assert by_name["日志记录"]["status"] == "ok"


# =============================================================================
# 5. 收件人同步：装载登录态 + 缓存火花/头像
# =============================================================================


class TestContactsSync:
    def test_sync_reads_friends_and_caches_profiles(self, tmp_path: Any) -> None:
        app = create_app(_settings(tmp_path))
        engine = FakeNavEngine(_friend_page())
        app.state.keeper.get_engine = lambda: engine  # type: ignore[method-assign]
        client = TestClient(app)

        resp = client.post("/api/contacts/sync", json={"limit": 80, "merge": True}, headers=HDR)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["synced"] == 3

        names = [c["name"] for c in body["contacts"]]
        assert names == ["阿明", "阿豪", "阿糕"]

        by_name = {c["name"]: c for c in body["contacts"]}
        assert by_name["阿豪"]["streak"] == "127"
        assert by_name["阿豪"]["has_avatar"] is True
        assert by_name["阿糕"]["streak"] == "重燃中 2/3"

        # 档案被缓存（火花 + 头像引用），后续 GET /api/contacts 也带
        profiles = Repository(tmp_path / "data").load_friend_profiles()
        assert profiles["阿豪"]["streak"] == "127"
        assert profiles["阿豪"]["avatar_ext"] == "png"
        # 头像字节落了独立文件，不再内嵌 base64
        repo = app.state.keeper.repository
        assert (repo.avatar_dir / f"{repo._avatar_key('阿豪')}.png").is_file()

        listed = client.get("/api/contacts", headers=HDR).json()["contacts"]
        listed_by_name = {c["name"]: c for c in listed}
        assert listed_by_name["阿明"]["streak"] == "849"
        assert listed_by_name["阿明"]["has_avatar"] is True

    def test_synced_friends_are_disabled_by_default(self, tmp_path: Any) -> None:
        """同步进来的好友默认「停用」——否则一次同步就会让定时任务给所有人群发。"""
        app = create_app(_settings(tmp_path))
        app.state.keeper.get_engine = lambda: FakeNavEngine(_friend_page())  # type: ignore[method-assign]
        client = TestClient(app)

        client.post("/api/contacts/sync", json={"limit": 80, "merge": True}, headers=HDR)

        contacts = Repository(tmp_path / "data").load_contacts()
        assert contacts, "应当同步出了联系人"
        assert all(c.enabled is False for c in contacts)

    def test_resync_keeps_existing_enabled_state(self, tmp_path: Any) -> None:
        """已存在且已启用的联系人，重新同步不该被改回停用。"""
        repo = Repository(tmp_path / "data")
        repo.ensure_layout()
        repo.upsert_contact(Contact(name="阿豪", enabled=True))

        app = create_app(_settings(tmp_path))
        app.state.keeper.get_engine = lambda: FakeNavEngine(_friend_page())  # type: ignore[method-assign]
        client = TestClient(app)
        client.post("/api/contacts/sync", json={"limit": 80, "merge": True}, headers=HDR)

        contacts = {c.name: c for c in Repository(tmp_path / "data").load_contacts()}
        assert contacts["阿豪"].enabled is True

    def test_sync_without_login_state_returns_502(self, tmp_path: Any) -> None:
        """登录态不可用时如实报错，而不是静默返回空列表。"""
        app = create_app(_settings(tmp_path))
        app.state.keeper.get_engine = lambda: FakeNavEngine(_friend_page(), logged_in=False)  # type: ignore[method-assign]
        client = TestClient(app)

        resp = client.post("/api/contacts/sync", json={"limit": 80}, headers=HDR)
        assert resp.status_code == 502
        assert "打开会话页失败" in resp.json()["detail"]


# =============================================================================
# 6. 群聊识别 + 前端禁缓存
# =============================================================================


def _page_with(rows: list[Node], avatars: dict[str, bytes] | None = None) -> Page:
    return Page(children={'[data-e2e="conversation-item"]': rows}, avatar_bodies=avatars or {})


class TestGroupSupport:
    def test_details_flags_group_by_name_suffix(self) -> None:
        page = _page_with(
            [
                _row("群聊甲（5）", "12", "https://cdn.example/g.png"),
                _row("单聊好友", "8", "https://cdn.example/h.png"),
            ]
        )
        details = navigator.read_conversation_details(page)
        assert [d["is_group"] for d in details] == [True, False]

    def test_details_flags_group_by_marker_element(self) -> None:
        group_row = Node(
            "项目群 3 刚刚",
            children={
                '[class*="ConversationItemtitle"]': [Node("项目群")],
                '[class*="Streak"]': [Node("3")],
                '[class*="GroupAvatar"]': [Node("")],
            },
        )
        details = navigator.read_conversation_details(_page_with([group_row]))
        assert details[0]["is_group"] is True

    def test_sync_persists_group_flag(self, tmp_path: Any) -> None:
        page = _page_with(
            [_row("群聊甲（5）", "3", "https://cdn.example/g.png")],
            {"https://cdn.example/g.png": b"G"},
        )
        app = create_app(_settings(tmp_path))
        app.state.keeper.get_engine = lambda: FakeNavEngine(page)  # type: ignore[method-assign]
        client = TestClient(app)

        client.post("/api/contacts/sync", json={"limit": 80, "merge": True}, headers=HDR)
        contacts = {c.name: c for c in Repository(tmp_path / "data").load_contacts()}
        assert contacts["群聊甲（5）"].is_group is True


class TestFrontendNoCache:
    def test_js_and_index_are_not_cached(self, tmp_path: Any) -> None:
        """前端资源必须禁缓存，否则改了 app.js 用户刷新还是旧的。"""
        client = TestClient(create_app(_settings(tmp_path)))
        for path in ("/app.js", "/style.css"):
            resp = client.get(path)
            assert "no-store" in resp.headers.get("Cache-Control", ""), path

        index = client.get("/")
        assert "no-store" in index.headers.get("Cache-Control", "")


# =============================================================================
# 辅助
# =============================================================================


def _patch_dispatcher(monkeypatch: pytest.MonkeyPatch, *, ok: bool) -> None:
    """把 Dispatcher 换成固定结果的替身，避免真的走网络。"""

    class _FakeDispatcher:
        @classmethod
        def from_settings(cls, settings: Any) -> _FakeDispatcher:
            return cls()

        def dispatch(self, alert: Any, *, minimum_level: Any = None) -> DispatchReport:
            result = DeliveryResult(
                channel="webhook", ok=ok, detail="HTTP 200" if ok else "网络错误：连不上", attempt=1
            )
            return DispatchReport(level=alert.level, attempted=(result,))

    monkeypatch.setattr("douyin_huohua_keeper.notify.Dispatcher", _FakeDispatcher)


# =============================================================================
# 7. 会话列表懒加载：滚动才能读全
# =============================================================================


class VirtualLoc:
    """带 ``evaluate`` 的定位器 —— evaluate 表示「滚动一屏」。"""

    def __init__(self, page: Any, nodes: list[Node]) -> None:
        self.page = page
        self._nodes = nodes

    def count(self) -> int:
        return len(self._nodes)

    @property
    def first(self) -> VirtualLoc:
        return VirtualLoc(self.page, self._nodes[:1])

    def nth(self, index: int) -> VirtualLoc:
        return VirtualLoc(self.page, self._nodes[index : index + 1])

    def evaluate(self, script: str) -> Any:
        return self.page.scroll_step()

    def inner_text(self, **kw: Any) -> str:
        return self._nodes[0].text if self._nodes else ""

    def get_attribute(self, name: str) -> str | None:
        return self._nodes[0].attrs.get(name) if self._nodes else None

    def locator(self, selector: str) -> VirtualLoc:
        out: list[Node] = []
        for node in self._nodes:
            out.extend(node.children.get(selector, []))
        return VirtualLoc(self.page, out)


class VirtualPage:
    """模拟**虚拟列表**：DOM 里永远只有一屏（viewport）那么多行，滚动时换内容。

    这是抖音网页端真实的行为 —— 所以 `count()` 永远不会变大，
    只能靠「边滚边采集」才能读全。
    """

    def __init__(self, all_rows: list[Node], viewport: int = 3) -> None:
        self.all_rows = all_rows
        self.viewport = viewport
        self.start = 0

    def scroll_step(self) -> dict[str, int]:
        before = self.start
        last_start = max(0, len(self.all_rows) - self.viewport)
        self.start = min(self.start + self.viewport, last_start)
        return {"before": before, "after": self.start, "scrollHeight": len(self.all_rows)}

    @property
    def visible(self) -> list[Node]:
        return self.all_rows[self.start : self.start + self.viewport]

    def locator(self, selector: str) -> VirtualLoc:
        if selector == '[data-e2e="conversation-item"]':
            return VirtualLoc(self, self.visible)
        return VirtualLoc(self, [])

    def wait_for_timeout(self, ms: int) -> None:
        pass


class TestVirtualConversationList:
    def test_collects_whole_list_across_scrolls(self) -> None:
        """虚拟列表必须边滚边采集 —— 只读一次只有首屏那几个。"""
        rows = [_row(f"好友{i}", str(i), f"u{i}") for i in range(1, 9)]  # 8 个好友
        page = VirtualPage(rows, viewport=3)

        details = navigator.collect_conversation_details(page, limit=100, pause_ms=0)

        assert [d["name"] for d in details] == [f"好友{i}" for i in range(1, 9)]

    def test_first_screen_only_when_expand_is_false(self) -> None:
        rows = [_row(f"好友{i}", str(i), f"u{i}") for i in range(1, 9)]
        page = VirtualPage(rows, viewport=3)

        details = navigator.read_conversation_details(page, limit=100, expand=False)

        assert [d["name"] for d in details] == ["好友1", "好友2", "好友3"]

    def test_reads_friends_beyond_first_screen(self) -> None:
        """首屏之外的好友也要能读到 —— 这正是用户「有些好友不在收件人里」的根因。"""
        rows = [
            _row("甲", "1", "u1"),
            _row("乙", "2", "u2"),
            _row("阿强", "455", "u3"),
            _row("阿糕", "6天后消失", "u4"),
        ]
        page = VirtualPage(rows, viewport=2)

        details = navigator.read_conversation_details(page, limit=100)

        assert [d["name"] for d in details] == ["甲", "乙", "阿强", "阿糕"]
        assert details[2]["streak"] == "455"


class TestGroupTextSignal:
    def test_group_detected_by_read_receipt_text(self) -> None:
        """群会话预览里的「1人已读 / [有人@我]」是单聊不会有的字样。"""
        row = Node(
            "阿明, 阿怪 291 1人已读 · [有人@我]",
            children={
                '[class*="ConversationItemtitle"]': [Node("阿明, 阿怪")],
                '[class*="Streak"]': [Node("291")],
            },
        )
        details = navigator.read_conversation_details(_page_with([row]), expand=False)
        assert details[0]["is_group"] is True

    def test_group_detected_by_comma_joined_member_names(self) -> None:
        """群名被渲染成「张三, 李四, 王五」这种多人并列时也要认出来。"""
        row = Node(
            "阿明, 阿怪, 阿乐, 阿甜 291 昨天 10:38",
            children={
                '[class*="ConversationItemtitle"]': [Node("阿明, 阿怪, 阿乐, 阿甜")],
                '[class*="Streak"]': [Node("291")],
            },
        )
        details = navigator.read_conversation_details(_page_with([row]), expand=False)
        assert details[0]["is_group"] is True
        assert details[0]["streak"] == "291"


# =============================================================================
# 8. 收件人分页 + 已启用优先
# =============================================================================


def _seed_contacts(tmp_path: Any, *, total: int, enabled: int = 0) -> Repository:
    repo = Repository(tmp_path / "data")
    repo.ensure_layout()
    for index in range(total):
        repo.upsert_contact(Contact(name=f"好友{index:02d}", enabled=index < enabled))
    return repo


class TestContactsPagination:
    def test_default_page_size_15_and_enabled_first(self, tmp_path: Any) -> None:
        _seed_contacts(tmp_path, total=20, enabled=3)
        client = TestClient(create_app(_settings(tmp_path)))

        data = client.get("/api/contacts", headers=HDR).json()

        assert data["count"] == 20
        assert data["pages"] == 2
        assert data["page"] == 1
        assert data["page_size"] == 15
        assert len(data["contacts"]) == 15  # 只发一页
        assert data["enabled_count"] == 3

        # 已启用的必须排在最前，其余在后
        flags = [c["enabled"] for c in data["contacts"]]
        assert flags[:3] == [True, True, True]
        assert not any(flags[3:])

    def test_second_page_has_the_rest(self, tmp_path: Any) -> None:
        _seed_contacts(tmp_path, total=20, enabled=3)
        client = TestClient(create_app(_settings(tmp_path)))

        data = client.get("/api/contacts?page=2&page_size=15", headers=HDR).json()
        assert data["page"] == 2
        assert len(data["contacts"]) == 5

    def test_page_out_of_range_is_clamped(self, tmp_path: Any) -> None:
        _seed_contacts(tmp_path, total=20)
        client = TestClient(create_app(_settings(tmp_path)))

        data = client.get("/api/contacts?page=99&page_size=15", headers=HDR).json()
        assert data["page"] == 2
        assert len(data["contacts"]) == 5

    def test_write_endpoint_returns_only_first_page(self, tmp_path: Any) -> None:
        """增删改也要只回第一页 —— 否则每次开关都回传上百人的 base64 头像。"""
        _seed_contacts(tmp_path, total=40)
        client = TestClient(create_app(_settings(tmp_path)))

        resp = client.post(
            "/api/contacts", json={"name": "新来的", "enabled": True}, headers=HDR
        ).json()
        assert resp["count"] == 41
        assert len(resp["contacts"]) == 15


# =============================================================================
# 9. 共享浏览器的空闲回收
# =============================================================================


def _app_state(tmp_path: Any, *, idle_timeout: int) -> AppState:
    base = load_settings()
    browser = dataclasses.replace(base.browser, engine_idle_timeout_sec=idle_timeout)
    return AppState(dataclasses.replace(base, data_dir=tmp_path / "data", browser=browser))


class TestEngineIdleRecycle:
    """共享浏览器空闲后要自动关闭 —— 否则它和定时任务的浏览器会同时活着（约 1.3GB）。"""

    def test_no_engine_is_a_noop(self, tmp_path: Any) -> None:
        assert _app_state(tmp_path, idle_timeout=600).maybe_reap_idle_engine(now=1e6) is False

    def test_reaps_after_idle_timeout(self, tmp_path: Any) -> None:
        state = _app_state(tmp_path, idle_timeout=600)
        calls = {"stopped": 0}
        state._engine = types.SimpleNamespace(
            stop=lambda: calls.__setitem__("stopped", calls["stopped"] + 1)
        )
        state._engine_last_used = 1000.0

        assert state.maybe_reap_idle_engine(now=1000.0 + 601) is True
        assert calls["stopped"] == 1
        assert state.peek_engine() is None

    def test_keeps_engine_when_recently_used(self, tmp_path: Any) -> None:
        state = _app_state(tmp_path, idle_timeout=600)
        state._engine = types.SimpleNamespace(stop=lambda: None)
        state._engine_last_used = 1000.0

        assert state.maybe_reap_idle_engine(now=1000.0 + 10) is False
        assert state.peek_engine() is not None

    def test_timeout_zero_disables_recycle(self, tmp_path: Any) -> None:
        state = _app_state(tmp_path, idle_timeout=0)
        state._engine = types.SimpleNamespace(stop=lambda: None)
        state._engine_last_used = 1.0

        assert state.maybe_reap_idle_engine(now=1e9) is False
        assert state.peek_engine() is not None

    def test_busy_engine_is_never_reaped(self, tmp_path: Any) -> None:
        """有调用正在跑时绝不能回收 —— 会把那次操作打断。"""
        state = _app_state(tmp_path, idle_timeout=600)
        calls = {"stopped": 0}
        state._engine = types.SimpleNamespace(
            stop=lambda: calls.__setitem__("stopped", calls["stopped"] + 1),
            session=types.SimpleNamespace(busy=True),
        )
        state._engine_last_used = 1.0

        assert state.maybe_reap_idle_engine(now=1e6) is False
        assert calls["stopped"] == 0
        assert state.peek_engine() is not None


# =============================================================================
# 10. 代码评审中修掉的高危 bug（回归）
# =============================================================================


class TestCliCheckMode:
    def test_check_mode_is_not_treated_as_usage_error(self) -> None:
        """回归：`--check` 曾经因 runpy 复用 sys.argv 变成 argparse 报错（退出码 2），自检从未真跑。"""
        from douyin_huohua_keeper import __main__ as cli
        from douyin_huohua_keeper import config as config_mod

        # ⚠️ 这个用例会真的走一遍 CLI：`_load_env()` 会把 .env 灌进 os.environ，
        # `_configure_logging()` 又会 refresh 配置缓存 —— 两者都会**污染后续用例**
        # （实测：后面的接口用例被 .env 里的 IP 白名单挡成 403）。
        # 所以这里整份快照、用完还原。
        env_snapshot = dict(os.environ)
        settings_snapshot = config_mod._settings
        try:
            code = cli.main(["--check"])
        finally:
            os.environ.clear()
            os.environ.update(env_snapshot)
            config_mod._settings = settings_snapshot

        assert isinstance(code, int)
        assert code != cli.EXIT_USAGE, "自检被当成参数错误 —— runpy 又复用 sys.argv 了"


class TestCookieRenewalJudgement:
    """回归：RENEW_REMINDER_DAYS(14) > CONSERVATIVE_LIFETIME_DAYS(7)，
    needs_renewal 恒为 True，「登录态正常」永远不可达。"""

    @staticmethod
    def _state_file(tmp_path: Path, *, age_days: float) -> Path:
        path = tmp_path / "main.state.json"
        path.write_text(
            json.dumps({"cookies": [{"name": "sessionid", "value": "x"}]}), encoding="utf-8"
        )
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def test_fresh_state_is_reported_normal(self, tmp_path: Path) -> None:
        status = check_cookie_freshness(self._state_file(tmp_path, age_days=0.1))

        assert status.present and status.session_present
        assert status.needs_renewal is False
        assert status.expired is False
        assert "正常" in status.detail

    def test_aging_state_asks_for_renewal(self, tmp_path: Path) -> None:
        status = check_cookie_freshness(self._state_file(tmp_path, age_days=5))

        assert status.needs_renewal is True
        assert status.expired is False

    def test_very_old_state_is_expired(self, tmp_path: Path) -> None:
        status = check_cookie_freshness(self._state_file(tmp_path, age_days=8))

        assert status.expired is True


class TestFileLockStaleness:
    """回归：以前「年龄超时」直接判残留，会夺走正在跑的任务的锁；release 也会删掉别人的锁。"""

    def test_live_holder_is_never_stolen(self, tmp_path: Path) -> None:
        path = tmp_path / ".run.lock"
        holder = FileLock(path, stale_seconds=0.0)  # 故意把超时设成 0
        holder.acquire()
        try:
            contender = FileLock(path, stale_seconds=0.0)
            with pytest.raises(LockBusyError):
                contender.acquire()
        finally:
            holder.release()

    def test_dead_holder_lock_can_be_taken_over(self, tmp_path: Path) -> None:
        path = tmp_path / ".run.lock"
        path.write_text(json.dumps({"pid": 999_999, "acquired_ts": time.time()}), encoding="utf-8")

        lock = FileLock(path)
        lock.acquire()
        try:
            assert lock.held
        finally:
            lock.release()

    def test_release_does_not_delete_other_holders_lock(self, tmp_path: Path) -> None:
        """ABA：自己的锁被夺走后，release 不能把新持有者的锁删掉。"""
        path = tmp_path / ".run.lock"
        mine = FileLock(path)
        mine.acquire()
        # 模拟「锁已被别人重建」
        path.write_text(json.dumps({"pid": 1, "acquired_ts": 12345.0}), encoding="utf-8")

        mine.release()

        assert path.exists(), "release 删掉了不属于自己的锁"


class TestReadJsonTolerance:
    def test_missing_file_returns_default(self, tmp_path: Path) -> None:
        assert read_json(tmp_path / "nope.json", default={"a": 1}) == {"a": 1}

    def test_corrupt_json_raises_corrupt_error(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("{不是 json", encoding="utf-8")

        with pytest.raises(CorruptFileError):
            read_json(path)

    def test_non_utf8_raises_corrupt_error(self, tmp_path: Path) -> None:
        path = tmp_path / "bin.json"
        path.write_bytes(b"\xff\xfe\x00{\x00")

        with pytest.raises(CorruptFileError):
            read_json(path)


# =============================================================================
# 11. 路径穿越（账号 ID / run ID 会被拼进文件名）
# =============================================================================


class TestIdPathTraversal:
    def test_account_state_path_rejects_traversal(self, tmp_path: Path) -> None:
        repo = Repository(tmp_path / "data")
        repo.ensure_layout()

        with pytest.raises(ValueError):
            repo.account_state_path("../../evil")

    def test_run_dir_rejects_traversal(self, tmp_path: Path) -> None:
        repo = Repository(tmp_path / "data")

        with pytest.raises(ValueError):
            repo.run_dir("..\\..\\windows")

    def test_load_report_returns_none_instead_of_reading_outside(self, tmp_path: Path) -> None:
        repo = Repository(tmp_path / "data")
        assert repo.load_report("../../etc/passwd") is None

    def test_clear_state_endpoint_returns_400(self, tmp_path: Any) -> None:
        client = TestClient(create_app(_settings(tmp_path)))

        resp = client.delete("/api/accounts/state?account_id=..%2F..%2Fevil", headers=HDR)

        assert resp.status_code == 400

    def test_get_run_returns_404_for_invalid_id(self, tmp_path: Any) -> None:
        client = TestClient(create_app(_settings(tmp_path)))

        resp = client.get("/api/runs/..%2F..%2Fetc", headers=HDR)

        assert resp.status_code == 404

    def test_normal_ids_still_work(self, tmp_path: Path) -> None:
        repo = Repository(tmp_path / "data")
        repo.ensure_layout()

        assert repo.account_state_path("main").name == "main.state.json"
        assert repo.run_dir("20260916-203000-ab12").name == "20260916-203000-ab12"


# =============================================================================
# 12. 重复发送防线：不确定的结果不能被重试；失败标记不能全页匹配
# =============================================================================


class TestUncertainIsNeverRetried:
    """``UNCERTAIN`` 的含义是「消息**可能已经发出**」。重试 = 重复发送。"""

    def test_uncertain_outcome_is_not_retried(self, tmp_path: Path) -> None:
        from douyin_huohua_keeper.common.errors import RetryBudget
        from douyin_huohua_keeper.engine.sender import SendOutcome
        from douyin_huohua_keeper.models import (
            FailureKind,
            Message,
            RunStatus,
            TaskPlan,
        )
        from douyin_huohua_keeper.scheduler.runner import _send_one

        contact = Contact(name="小明")
        plan = TaskPlan(
            task_id="t",
            targets=(contact,),
            messages=(Message(kind="text", content="1"),),
        )
        calls = {"n": 0}

        class _Engine:
            def send_to_contact(self, c: Any, m: Any, *, dry_run: bool, allow_search: bool) -> Any:
                calls["n"] += 1
                return SendOutcome(
                    contact_name=c.name,
                    status=RunStatus.UNCERTAIN,
                    failure_kind=FailureKind.TRANSIENT,
                    detail="输入框已清空但没能在会话列表确认到（可能已发出）",
                )

        repo = Repository(tmp_path / "data")
        repo.ensure_layout()

        outcome, aborted = _send_one(
            engine=_Engine(),
            contact=contact,
            plan=plan,
            settings=dataclasses.replace(load_settings(), data_dir=tmp_path / "data"),
            repo=repo,
            dry_run=False,
            retry_budget=RetryBudget(remaining=5),
            pick_message=lambda messages, rng: messages[0],
            rng=random.Random(0),
        )

        assert calls["n"] == 1, "UNCERTAIN 被重试了 —— 会造成重复发送"
        assert outcome.status is RunStatus.UNCERTAIN
        assert aborted is False


class TestFailureMarkerIsNotPageWide:
    """回归：选择器里曾有 ``[class*="error"]``/``[class*="failed"]`` 全页兜底，
    页面上任意带 error 类名的元素都会把**成功**判成失败 → 重试 → 重复发送。"""

    def test_broad_selectors_are_gone(self) -> None:
        from douyin_huohua_keeper.engine import selectors as sel

        assert '[class*="error"]' not in sel.MESSAGE_FAILED
        assert '[class*="failed"]' not in sel.MESSAGE_FAILED

    def test_unrelated_error_element_is_not_a_failure(self) -> None:
        from douyin_huohua_keeper.engine import confirmer

        page = Page(children={'[class*="error"]': [Node("某广告位加载失败")]})

        assert confirmer._has_failure_marker(page) is False

    def test_explicit_send_failure_marker_is_detected(self) -> None:
        from douyin_huohua_keeper.engine import confirmer

        page = Page(children={'[class*="sendFailed"]': [Node("重新发送")]})

        assert confirmer._has_failure_marker(page) is True


class TestEngineRespectsOutcomeContract:
    """引擎层对外的契约是「失败一律返回 SendOutcome，不抛异常」。"""

    def test_unstarted_engine_returns_outcome(self) -> None:
        from douyin_huohua_keeper.engine.sender import Engine
        from douyin_huohua_keeper.models import Message, RunStatus

        base = load_settings()
        engine = Engine(base.browser, base.send)

        outcome = engine.send_to_contact(Contact(name="小明"), Message(kind="text", content="1"))

        assert outcome.status is RunStatus.FAILED
        assert outcome.failure_kind is not None
        assert "EngineError" in outcome.detail

    def test_browser_error_becomes_failed_outcome(self) -> None:
        from douyin_huohua_keeper.engine.browser import BrowserError
        from douyin_huohua_keeper.engine.sender import Engine
        from douyin_huohua_keeper.models import Message, RunStatus

        class _Boom(Engine):
            @property
            def session(self) -> Any:
                raise BrowserError("浏览器线程没了")

        engine = _Boom.__new__(_Boom)

        outcome = engine.send_to_contact(Contact(name="小明"), Message(kind="text", content="1"))

        assert outcome.status is RunStatus.FAILED
        assert "BrowserError" in outcome.detail
