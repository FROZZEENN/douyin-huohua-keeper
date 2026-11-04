"""回归测试：运行中途被中止时，报告必须如实反映「谁没发出去」。

对应 2026-11-03 的真实事故：

- 给「雪」重试前，``check_login()`` 因为没有兜住异常，把整个运行抛了出去；
- 4 个人之后的所有收件人一条都没发；
- 但报告里只有 4 条 success → summary 写成「成功 4/4，失败 0」，
  推送还告诉用户「所有收件人的消息都已发送成功，火花已续上」。

这两个洞都在这里堵住：
1. ``check_login()`` 抛异常时，最多影响当前这个人（浏览器整体卡死才中止本轮）；
2. 只要本轮中途终止，所有「没轮到」的收件人都要写进报告并标为失败。
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from douyin_huohua_keeper.config import load_settings
from douyin_huohua_keeper.engine.auth import LoginCheck
from douyin_huohua_keeper.engine.sender import SendOutcome
from douyin_huohua_keeper.models import (
    Contact,
    FailureKind,
    Message,
    RunStatus,
    TargetOutcome,
    TaskPlan,
)
from douyin_huohua_keeper.scheduler import runner as runner_mod
from douyin_huohua_keeper.store.repo import Repository


def _make_plan(names: tuple[str, ...]) -> TaskPlan:
    return TaskPlan(
        task_id="t",
        targets=tuple(Contact(name=n) for n in names),
        messages=(Message(kind="text", content="1"),),
    )


def _fresh_state(repo: Repository) -> Path:
    """写一份「刚登录过」的登录态，避免发送前的本地检查直接判过期。"""
    path = repo.account_state_path("main")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"cookies": [{"name": "sessionid", "value": "x"}], "origins": []}', "utf-8")
    return path


class TestLoginCheckFailure:
    """重试前的「顺手确认登录态」不许把整轮任务带崩。"""

    def _send(self, monkeypatch: Any, tmp_path: Path, *, error: Exception):
        from douyin_huohua_keeper.common import errors as err_mod
        from douyin_huohua_keeper.common.errors import RetryBudget

        # 重试前的退避不要真睡
        monkeypatch.setattr(err_mod, "sleep_with_logging", lambda *a, **k: None)

        class _Engine:
            def send_to_contact(self, c: Any, m: Any, *, dry_run: bool, allow_search: bool) -> Any:
                return SendOutcome(
                    contact_name=c.name,
                    status=RunStatus.FAILED,
                    failure_kind=FailureKind.TRANSIENT,
                    detail="找不到联系人「小明」",
                )

            def check_login(self) -> Any:
                raise error

        repo = Repository(tmp_path / "data")
        repo.ensure_layout()

        return runner_mod._send_one(
            engine=_Engine(),
            contact=Contact(name="小明"),
            plan=_make_plan(("小明",)),
            settings=dataclasses.replace(load_settings(), data_dir=tmp_path / "data"),
            repo=repo,
            dry_run=False,
            retry_budget=RetryBudget(remaining=5),
            pick_message=lambda messages, rng: messages[0],
            rng=__import__("random").Random(0),
        )

    def test_stuck_browser_aborts_the_run(self, monkeypatch: Any, tmp_path: Path) -> None:
        """浏览器被判卡死 → 真的要中止本轮（否则剩下十几个人会逐个空转 90 秒）。"""
        outcome, aborted = self._send(
            monkeypatch,
            tmp_path,
            error=RuntimeError("浏览器已判定卡死（上一次操作超时，任务仍占用着浏览器线程）"),
        )

        assert aborted is True
        assert outcome.status is RunStatus.FAILED
        assert "浏览器无响应" in (outcome.detail or "")

    def test_other_exception_only_fails_this_one(self, monkeypatch: Any, tmp_path: Path) -> None:
        """普通异常只影响这个人 —— 绝不能因为一次检查失败就丢掉后面所有人。"""
        outcome, aborted = self._send(monkeypatch, tmp_path, error=RuntimeError("网络抖了一下"))

        assert aborted is False
        assert outcome.status is RunStatus.FAILED
        assert "找不到联系人" in (outcome.detail or "")


class TestAbortedRunCoversEveryTarget:
    """中途终止时，报告必须包含所有人 —— 不允许「只发 4 人却报成功 4/4」。"""

    def _run(
        self,
        monkeypatch: Any,
        tmp_path: Path,
        *,
        names: tuple[str, ...],
        boom_on: str | None,
        login_ok: bool = True,
    ) -> dict[str, Any]:
        repo = Repository(tmp_path / "data")
        repo.ensure_layout()
        _fresh_state(repo)

        sent: list[str] = []

        class FakeEngine:
            def __init__(self, *a: Any, **k: Any) -> None:
                pass

            def start(self) -> None:
                pass

            def stop(self) -> None:
                pass

            def load_state(self, path: Any) -> None:
                pass

            def check_login(self) -> Any:
                return LoginCheck(
                    ok=login_ok,
                    reason="" if login_ok else "AUTH_EXPIRED",
                    detail="会话页正常" if login_ok else "登录态已过期",
                )

            def send_to_contact(self, contact: Any, message: Any, **kw: Any) -> Any:
                if contact.name == boom_on:
                    raise RuntimeError("模拟：浏览器在发送途中崩了")
                sent.append(contact.name)
                return SendOutcome(
                    contact_name=contact.name,
                    status=RunStatus.SUCCESS,
                    sent_count=1,
                    detail="已确认送达",
                )

        captured: dict[str, Any] = {}

        def fake_finalize(**kwargs: Any) -> Any:
            captured["outcomes"] = kwargs["outcomes"]
            captured["abort_reason"] = kwargs["abort_reason"]
            return None

        # 引擎是 _execute 内部 `from ..engine import Engine` 导入的
        monkeypatch.setattr("douyin_huohua_keeper.engine.Engine", FakeEngine)
        monkeypatch.setattr(runner_mod, "_finalize", fake_finalize)
        monkeypatch.setattr(
            runner_mod,
            "runstate",
            type(
                "S",
                (),
                {
                    "begin": staticmethod(lambda **k: None),
                    "finish": staticmethod(lambda: None),
                    "set_stage": staticmethod(lambda *a, **k: None),
                    "start_target": staticmethod(lambda *a, **k: None),
                    "mark_outcome": staticmethod(lambda *a, **k: None),
                },
            )(),
            raising=False,
        )

        settings = dataclasses.replace(load_settings(), data_dir=tmp_path / "data")
        runner_mod._execute(
            settings,
            repo,
            _make_plan(names),
            runner_mod.RunContext(
                run_id="20261103-093000-test",
                started_at="2026-11-03T09:30:00",
                dry_run=False,
                target_count=len(names),
            ),
            False,
            kind="scheduled",
        )
        captured["sent"] = sent
        return captured

    def test_crash_midway_marks_remaining_as_failed(self, monkeypatch: Any, tmp_path: Path) -> None:
        """第 2 个人崩掉 → 第 2、3 人都必须出现在报告里（而不是只报第 1 个成功）。"""
        result = self._run(monkeypatch, tmp_path, names=("甲", "乙", "丙"), boom_on="乙")

        outcomes: list[TargetOutcome] = result["outcomes"]
        by_name = {o.name: o for o in outcomes}

        assert set(by_name) == {"甲", "乙", "丙"}, "有收件人被漏掉了 —— 正是 11-03 的 bug"
        assert by_name["甲"].status is RunStatus.SUCCESS
        assert by_name["乙"].status is RunStatus.FAILED
        assert by_name["丙"].status is RunStatus.FAILED
        assert "没有尝试发送" in (by_name["丙"].detail or "")
        assert result["abort_reason"], "中途崩溃必须留下中止原因"

    def test_abort_before_any_send_covers_all(self, monkeypatch: Any, tmp_path: Path) -> None:
        """登录态过期、一个人都没轮到 → 所有人都要标失败（不能报「成功 0/0」）。"""
        result = self._run(monkeypatch, tmp_path, names=("甲", "乙"), boom_on=None, login_ok=False)

        outcomes: list[TargetOutcome] = result["outcomes"]
        assert len(outcomes) == 2
        assert all(o.status is RunStatus.FAILED for o in outcomes)
        assert all("没有尝试发送" in (o.detail or "") for o in outcomes)

    def test_clean_run_has_no_extra_entries(self, monkeypatch: Any, tmp_path: Path) -> None:
        """一切正常时不能凭空多出「未发送」条目。"""
        result = self._run(monkeypatch, tmp_path, names=("甲", "乙"), boom_on=None)

        outcomes: list[TargetOutcome] = result["outcomes"]
        assert len(outcomes) == 2
        assert all(o.status is RunStatus.SUCCESS for o in outcomes)
        assert result["abort_reason"] is None
        assert result["sent"] == ["甲", "乙"]
