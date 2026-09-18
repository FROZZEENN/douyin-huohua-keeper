"""workbench 层的单元测试：鉴权中间件 + REST 路由。

要点：
- 不走 lifespan（不 ``with TestClient``），避免把真调度器拉起来 ——
  这里只测 HTTP 层的行为。
- 应用通过 ``create_app(settings)`` 构造，settings 指向临时数据目录，
  所以对路由的读写不会碰真实 ``data/``。
- 鉴权中间件是这套测试的重心：它是保护二维码和配置的唯一防线。
"""

from __future__ import annotations

import dataclasses
import time
import types
from typing import Any

import pytest
from fastapi.testclient import TestClient

from douyin_huohua_keeper import __version__
from douyin_huohua_keeper.config import load_settings
from douyin_huohua_keeper.workbench.app import create_app

TOKEN = "unit-test-token-0123456789abcdef"


def _settings(tmp_path: Any, *, token: str = TOKEN, allowed_ips: tuple[str, ...] = ()) -> Any:
    base = load_settings()
    workbench = dataclasses.replace(
        base.workbench, token=token, allowed_ips=allowed_ips
    )
    return dataclasses.replace(base, data_dir=tmp_path / "data", workbench=workbench)


@pytest.fixture
def client(tmp_path: Any) -> TestClient:
    app = create_app(_settings(tmp_path))
    return TestClient(app)


@pytest.fixture
def open_client(tmp_path: Any) -> TestClient:
    """未配置令牌的应用（本地自用的默认状态）。"""
    app = create_app(_settings(tmp_path, token=""))
    return TestClient(app)


def _authed(client: TestClient, **kwargs: Any) -> Any:
    kwargs.setdefault("headers", {"X-Huohua-Token": TOKEN})
    return client.get(**kwargs)


# =============================================================================
# 鉴权中间件
# =============================================================================


class TestAuthMiddleware:
    def test_missing_token_is_401(self, client: TestClient) -> None:
        resp = client.get("/api/contacts")
        assert resp.status_code == 401
        assert resp.json()["error"] == "unauthorized"
        assert resp.headers["WWW-Authenticate"] == "Bearer"

    def test_wrong_token_is_401(self, client: TestClient) -> None:
        resp = client.get("/api/contacts", headers={"X-Huohua-Token": "wrong"})
        assert resp.status_code == 401

    def test_token_via_header(self, client: TestClient) -> None:
        assert client.get(
            "/api/contacts", headers={"X-Huohua-Token": TOKEN}
        ).status_code == 200

    def test_token_via_bearer(self, client: TestClient) -> None:
        assert client.get(
            "/api/contacts", headers={"Authorization": f"Bearer {TOKEN}"}
        ).status_code == 200

    def test_token_via_query_param(self, client: TestClient) -> None:
        assert client.get(f"/api/contacts?token={TOKEN}").status_code == 200

    def test_health_is_public(self, client: TestClient) -> None:
        """/api/health 必须免鉴权 —— Docker healthcheck 不带令牌。"""
        assert client.get("/api/health").status_code == 200

    def test_index_is_public(self, client: TestClient) -> None:
        """/ 是前端页面本身，是输入令牌的地方，必须放行。"""
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 200

    def test_double_slash_also_public(self, client: TestClient) -> None:
        """/ 这种写法不该掉进 401 JSON —— 回归：曾有人打开站点看到一段 JSON。"""
        resp = client.get("//", follow_redirects=False)
        assert resp.status_code == 200

    def test_static_files_are_public(self, client: TestClient) -> None:
        resp = client.get("/app.js")
        assert resp.status_code == 200
        # Windows 注册表常把 .js 标成 text/plain，会导致 nosniff 拒绝执行
        assert resp.headers["content-type"].startswith("text/javascript")

    def test_version_requires_token(self, client: TestClient) -> None:
        assert client.get("/api/version").status_code == 401
        resp = client.get("/api/version", headers={"X-Huohua-Token": TOKEN})
        assert resp.status_code == 200
        assert resp.json()["version"] == __version__

    def test_no_token_configured_allows_everything(self, open_client: TestClient) -> None:
        """"未配令牌 = 不拦任何请求"是刻意的开箱即用默认。"""
        assert open_client.get("/api/contacts").status_code == 200


class TestIpAllowlist:
    def test_ip_not_in_allowlist_is_403(self, tmp_path: Any) -> None:
        app = create_app(_settings(tmp_path, allowed_ips=("10.0.0.0/8",)))
        client = TestClient(app)
        # TestClient 的 client.host 是 "testclient"，不是合法 IP → 拒绝
        resp = client.get("/api/contacts", headers={"X-Huohua-Token": TOKEN})
        assert resp.status_code == 403
        assert resp.json()["error"] == "ip_not_allowed"

    def test_allowlist_applies_even_with_valid_token(self, tmp_path: Any) -> None:
        """白名单检查在令牌之前 —— 令牌对不能挡住「IP 本身就不对」的请求。"""
        app = create_app(_settings(tmp_path, allowed_ips=("10.0.0.0/8",)))
        client = TestClient(app)
        assert (
            client.get("/api/contacts", headers={"X-Huohua-Token": TOKEN}).status_code
            == 403
        )

    def test_empty_allowlist_means_no_ip_restriction(self, client: TestClient) -> None:
        assert client.get(
            "/api/contacts", headers={"X-Huohua-Token": TOKEN}
        ).status_code == 200


# =============================================================================
# 收件人 CRUD
# =============================================================================


class TestContactsCrud:
    def test_list_starts_empty(self, client: TestClient) -> None:
        data = _authed(client, url="/api/contacts").json()
        assert data["contacts"] == []
        assert data["count"] == 0

    def test_add_and_list(self, client: TestClient) -> None:
        resp = client.post(
            "/api/contacts",
            json={"name": "阿明", "note": "用户自己"},
            headers={"X-Huohua-Token": TOKEN},
        )
        assert resp.status_code == 200
        assert resp.json()["created"] is True

        data = client.get(
            "/api/contacts", headers={"X-Huohua-Token": TOKEN}
        ).json()
        assert data["count"] == 1
        assert data["contacts"][0]["name"] == "阿明"
        assert data["contacts"][0]["sent_today"] is False

    def test_add_same_name_updates_not_duplicates(self, client: TestClient) -> None:
        headers = {"X-Huohua-Token": TOKEN}
        client.post("/api/contacts", json={"name": "阿明"}, headers=headers)
        resp = client.post(
            "/api/contacts", json={"name": "阿明", "note": "改备注"}, headers=headers
        )
        assert resp.json()["created"] is False

        data = client.get("/api/contacts", headers=headers).json()
        assert data["count"] == 1
        assert data["contacts"][0]["note"] == "改备注"

    def test_add_empty_name_is_422(self, client: TestClient) -> None:
        resp = client.post(
            "/api/contacts", json={"name": "  "}, headers={"X-Huohua-Token": TOKEN}
        )
        assert resp.status_code == 422

    def test_patch_renames(self, client: TestClient) -> None:
        headers = {"X-Huohua-Token": TOKEN}
        client.post("/api/contacts", json={"name": "旧名"}, headers=headers)

        resp = client.patch(
            "/api/contacts/旧名", json={"name": "新名"}, headers=headers
        )
        assert resp.status_code == 200

        names = [c["name"] for c in client.get("/api/contacts", headers=headers).json()["contacts"]]
        assert names == ["新名"]

    def test_patch_missing_is_404(self, client: TestClient) -> None:
        resp = client.patch(
            "/api/contacts/不存在", json={"name": "x"}, headers={"X-Huohua-Token": TOKEN}
        )
        assert resp.status_code == 404

    def test_delete(self, client: TestClient) -> None:
        headers = {"X-Huohua-Token": TOKEN}
        client.post("/api/contacts", json={"name": "要删的"}, headers=headers)

        assert (
            client.delete("/api/contacts/要删的", headers=headers).status_code == 200
        )
        assert client.delete(
            "/api/contacts/要删的", headers=headers
        ).status_code == 404

    def test_reset_today_requires_confirm(self, client: TestClient) -> None:
        headers = {"X-Huohua-Token": TOKEN}
        client.post("/api/contacts", json={"name": "阿明"}, headers=headers)

        # 不带 confirm 必须被 422 拦下 —— 清了标记就可能重复发送
        resp = client.post("/api/contacts/reset-today", json={}, headers=headers)
        assert resp.status_code == 422

    def test_reset_today_clears_marker(self, client: TestClient) -> None:
        headers = {"X-Huohua-Token": TOKEN}
        client.post("/api/contacts", json={"name": "阿明"}, headers=headers)

        # 先把标记造出来（模拟「今天已发过」）
        client.patch(
            "/api/contacts/阿明",
            json={"name": "阿明", "note": None},
            headers=headers,
        )
        resp = client.post(
            "/api/contacts/reset-today",
            json={"names": [], "confirm": True},
            headers=headers,
        )
        assert resp.status_code == 200


# =============================================================================
# 只读端点
# =============================================================================


class TestReadOnlyEndpoints:
    def test_tasks_reflect_tasks_json(self, client: TestClient, tmp_path: Any) -> None:
        from douyin_huohua_keeper.models import Schedule
        from douyin_huohua_keeper.store.repo import Repository

        repo = Repository(tmp_path / "data")
        repo.save_tasks(
            {
                **repo.load_tasks(),
                "messages": [{"kind": "text", "content": "1"}],
            }
        )
        repo.save_schedule(Schedule(enabled=True, hour=9, minute=30, jitter_minutes=0))

        data = client.get(
            "/api/tasks", headers={"X-Huohua-Token": TOKEN}
        ).json()
        assert [m["content"] for m in data["messages"]] == ["1"]

    def test_schedule_endpoint(self, client: TestClient, tmp_path: Any) -> None:
        from douyin_huohua_keeper.models import Schedule
        from douyin_huohua_keeper.store.repo import Repository

        Repository(tmp_path / "data").save_schedule(
            Schedule(enabled=True, hour=9, minute=30, jitter_minutes=0)
        )

        data = client.get(
            "/api/tasks/schedule", headers={"X-Huohua-Token": TOKEN}
        ).json()
        assert (data["hour"], data["minute"]) == (9, 30)

    def test_runs_starts_empty(self, client: TestClient) -> None:
        resp = client.get("/api/runs", headers={"X-Huohua-Token": TOKEN})
        assert resp.status_code == 200

    def test_runs_days(self, client: TestClient) -> None:
        resp = client.get("/api/runs/days", headers={"X-Huohua-Token": TOKEN})
        assert resp.status_code == 200

    def test_account_without_login(self, client: TestClient) -> None:
        """还没绑定账号时，账号页要能正常返回「未登录」而不是 500。"""
        resp = client.get("/api/accounts", headers={"X-Huohua-Token": TOKEN})
        assert resp.status_code == 200


# =============================================================================
# 扫码轮询：单次失败必须**可重试**，不能变成终态
# =============================================================================


class TestQrPollFailureIsRetryable:
    """回归（实测）：一次瞬时失败曾把整个扫码轮询永久停掉。

    现象：用户扫了码、手机上也点了确认，界面却永远停在「等待扫码」，
    二维码图不动、也没有任何地方可以做二次验证。

    根因：``/qr/poll`` 出错时返回 ``state="error"``，而前端一看到 ``error``
    就 ``stopQrPoll()`` —— 于是**一次**瞬时失败（浏览器忙 / 操作超时 /
    网络抖一下）就把轮询杀死了，之后再也不会恢复。

    所以这个接口出错时只能回**可重试**的状态，前端也必须继续轮询。
    """

    def _app_with_session(self, tmp_path: Any) -> Any:
        app = create_app(_settings(tmp_path))
        app.state.keeper.qr_session = {
            "account_id": "main",
            "started_at": time.time(),
            "saved": False,
        }
        return app

    def test_engine_failure_is_retryable_not_terminal(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = self._app_with_session(tmp_path)

        def boom() -> Any:
            raise RuntimeError("浏览器操作超时（90s）")

        monkeypatch.setattr(app.state.keeper, "get_engine", boom)
        client = TestClient(app)

        resp = _authed(client, url="/api/accounts/qr/poll")

        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "retrying"
        assert body["state"] != "error", "error 会让前端停掉轮询，用户就再也等不到反应"

    def test_no_active_session_is_idle(self, tmp_path: Any) -> None:
        client = TestClient(create_app(_settings(tmp_path)))

        body = _authed(client, url="/api/accounts/qr/poll").json()

        assert body["state"] == "idle"


class TestQrActForwardsFeedback:
    """``/qr/act`` 必须把「写没写进去」的结论转发给前端。

    回归：后端曾漏转发 ``verified`` —— 前端拿不到结论，就没法把失败标红，
    于是「其实没写进去」和「写进去了」在用户眼里一模一样
    （用户的原话：「点了没用」）。
    """

    class _Session:
        def __init__(self) -> None:
            self.calls = 0

        def call(self, fn: Any, timeout: Any = None) -> Any:
            self.calls += 1
            if self.calls == 1:  # 第一次 = 执行动作
                return {
                    "detail": "已写入「1234」到请输入验证码（直接填入）",
                    "verified": True,
                    "actual": "1234",
                    "target": {"index": 2, "placeholder": "请输入验证码"},
                    "last_click": None,
                }
            return {  # 第二次 = 重新截图
                "url": "https://www.douyin.com/chat",
                "width": 1440,
                "height": 900,
                "image": "data:image/jpeg;base64,x",
                "inputs": [],
                "code_index": None,
            }

    def test_verified_is_forwarded(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        app = create_app(_settings(tmp_path))
        app.state.keeper.qr_session = {
            "account_id": "main",
            "started_at": time.time(),
            "saved": False,
        }
        engine = types.SimpleNamespace(session=self._Session())
        monkeypatch.setattr(app.state.keeper, "get_engine", lambda: engine)
        client = TestClient(app)

        resp = client.post(
            "/api/accounts/qr/act",
            json={"action": "type", "text": "1234", "index": 2},
            headers={"X-Huohua-Token": TOKEN},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["verified"] is True
        assert body["actual"] == "1234"
        assert body["target"]["placeholder"] == "请输入验证码"

    def test_act_without_session_is_409(self, tmp_path: Any) -> None:
        client = TestClient(create_app(_settings(tmp_path)))

        resp = client.post(
            "/api/accounts/qr/act",
            json={"action": "type", "text": "1"},
            headers={"X-Huohua-Token": TOKEN},
        )

        assert resp.status_code == 409
