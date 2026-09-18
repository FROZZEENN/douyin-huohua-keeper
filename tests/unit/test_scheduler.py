"""scheduler 层的单元测试。

覆盖两块：
- ``runner``：任务计划的冻结（``_build_plan``）与执行编排（``run_once``）
- ``jobs``：下次触发时刻的计算（``_next_trigger``）与**配置来源统一**
  （tasks.json 优先于环境变量 —— 这个不一致实测会让用户改了时间不生效）
"""

from __future__ import annotations

import dataclasses
import types
from datetime import datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import pytest

from douyin_huohua_keeper.config import load_settings
from douyin_huohua_keeper.models import (
    AlertLevel,
    Contact,
    FailureKind,
    Message,
    RunStatus,
    Schedule,
    TargetOutcome,
)
from douyin_huohua_keeper.scheduler import jobs as jobs_mod
from douyin_huohua_keeper.scheduler.jobs import _humanize, _next_daily_trigger, _next_trigger
from douyin_huohua_keeper.scheduler.runner import (
    _build_plan,
    resolve_prevent_duplicates,
    run_once,
)
from douyin_huohua_keeper.store.repo import Repository, today_str

TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def repo(tmp_path: Any) -> Repository:
    """一个落在临时目录、已初始化目录结构的仓库。"""
    settings = load_settings()
    object.__setattr__(settings, "data_dir", tmp_path) if False else None
    r = Repository(tmp_path / "data")
    r.ensure_layout()
    return r


# =============================================================================
# _build_plan：任务计划的冻结
# =============================================================================


class TestBuildPlan:
    def test_returns_none_without_contacts(self, repo: Repository) -> None:
        settings = load_settings()
        assert _build_plan(repo, settings) is None

    def test_includes_enabled_contacts(self, repo: Repository) -> None:
        repo.upsert_contact(Contact(name="小明"))
        repo.save_tasks(
            {
                **repo.load_tasks(),
                "messages": [{"kind": "text", "content": "早"}],
            }
        )
        settings = load_settings()

        plan = _build_plan(repo, settings)

        assert plan is not None
        assert [c.name for c in plan.targets] == ["小明"]
        assert [m.content for m in plan.messages] == ["早"]

    def test_filters_disabled_contacts(self, repo: Repository) -> None:
        repo.upsert_contact(Contact(name="启用", enabled=True))
        repo.upsert_contact(Contact(name="停用", enabled=False))
        settings = load_settings()

        plan = _build_plan(repo, settings)

        assert plan is not None
        assert [c.name for c in plan.targets] == ["启用"]

    def test_contacts_override_sends_exactly_selected(self, repo: Repository) -> None:
        """override 是用户逐个勾选的，**不过滤 enabled**。

        语义变更背景：群发页把新好友存成「停用」（防止被每日定时任务误发），
        但手动指定发送时必须有效 —— 勾选本身就是明确意图。
        定时任务不受影响（它仍走 enabled_contacts()）。
        """
        repo.upsert_contact(Contact(name="名单里的人"))
        repo.upsert_contact(Contact(name="停用", enabled=False))
        settings = load_settings()

        plan = _build_plan(
            repo,
            settings,
            contacts_override=(Contact(name="临时指定"), Contact(name="停用", enabled=False)),
        )

        assert plan is not None
        assert [c.name for c in plan.targets] == ["临时指定", "停用"]

    def test_messages_override_replaces_pool(self, repo: Repository) -> None:
        """临时消息替换文案池。

        ⚠️ 防重复**不再**从「有没有 messages_override」推断 ——
        那正是「手动点了发送却什么都没发」的事故成因。
        现在由调用方显式传入。
        """
        repo.upsert_contact(Contact(name="小明", last_sent_on="2099-01-01"))
        settings = load_settings()

        plan = _build_plan(
            repo,
            settings,
            contacts_override=(Contact(name="小明"),),
            messages_override=(Message(kind="text", content="1"),),
            prevent_duplicates=False,
        )

        assert plan is not None
        assert [m.content for m in plan.messages] == ["1"]
        # 手动明确点下的发送不该被「今天已经发过」拦住
        assert plan.prevent_duplicates is False

    def test_scheduled_run_keeps_dedupe(self, repo: Repository) -> None:
        """定时路径维持防重复不变 —— 这是防重复的主战场。"""
        repo.upsert_contact(Contact(name="小明"))
        settings = load_settings()

        plan = _build_plan(repo, settings)

        assert plan is not None
        assert plan.prevent_duplicates is True


class TestPreventDuplicatesPolicy:
    """防重复的开关只看「谁触发的」，不看别的间接条件。

    回归背景：判定依据曾经是「有没有传 messages_override」。于是工作台首页
    那个「立即发送一次」按钮（不传 message）被当成定时任务，点下去只得到
    「今天已经发送过（防重复）」的跳过记录 —— 用户以为发了，实际一条没出去。
    """

    def test_scheduled_kind_enables_dedupe(self) -> None:
        assert resolve_prevent_duplicates("scheduled", None) is True

    def test_manual_kind_disables_dedupe(self) -> None:
        assert resolve_prevent_duplicates("manual", None) is False

    def test_unknown_kind_disables_dedupe(self) -> None:
        """默认按「用户明确触发」处理 —— 宁可真发，不可静默跳过。"""
        assert resolve_prevent_duplicates("whatever", None) is False

    def test_explicit_value_wins(self) -> None:
        assert resolve_prevent_duplicates("scheduled", False) is False
        assert resolve_prevent_duplicates("manual", True) is True

    def test_uses_default_message_when_pool_empty(self, repo: Repository) -> None:
        repo.upsert_contact(Contact(name="小明"))
        settings = load_settings()

        plan = _build_plan(repo, settings)

        assert plan is not None
        assert plan.messages  # 至少有一条，否则任务无从执行
        assert plan.messages[0].content == "早"


# =============================================================================
# _next_trigger：下次触发时刻的计算
# =============================================================================


class TestNextTrigger:
    def test_result_is_in_the_future(self) -> None:
        schedule = Schedule(enabled=True, hour=10, minute=30, jitter_minutes=25)
        next_run = _next_trigger(schedule, TZ)
        assert next_run > datetime.now(TZ)

    def test_respects_hour_and_minute(self) -> None:
        now = datetime.now(TZ)
        schedule = Schedule(enabled=True, hour=10, minute=30, jitter_minutes=0)
        next_run = _next_trigger(schedule, TZ)

        assert next_run.hour == 10
        assert next_run.minute == 30
        if now.hour < 10 or (now.hour == 10 and now.minute < 30):
            assert next_run.date() == now.date()
        else:
            assert next_run.date() > now.date()

    def test_jitter_stays_within_bounds(self) -> None:
        schedule = Schedule(enabled=True, hour=10, minute=30, jitter_minutes=25)
        for _ in range(50):
            next_run = _next_trigger(schedule, TZ)
            base = next_run.replace(minute=30, second=0, microsecond=0)
            offset = (next_run - base).total_seconds() / 60
            assert 0 <= offset <= 25, f"抖动越界：{offset} 分钟"

    def test_zero_jitter_is_exact(self) -> None:
        schedule = Schedule(enabled=True, hour=23, minute=59, jitter_minutes=0)
        next_run = _next_trigger(schedule, TZ)
        assert next_run.second == 0
        assert next_run.minute == 59

    def test_returns_tomorrow_when_today_already_passed(self) -> None:
        # 取一个必然已经过去的时刻：用当前时间往前推
        now = datetime.now(TZ)
        schedule = Schedule(
            enabled=True,
            hour=max(0, now.hour - 1 if now.hour > 0 else 23),
            minute=now.minute,
            jitter_minutes=0,
        )
        next_run = _next_trigger(schedule, TZ)
        assert next_run > now


class TestHumanize:
    def test_seconds(self) -> None:
        assert _humanize(45) == "45 秒"

    def test_minutes(self) -> None:
        assert _humanize(60) == "1 分"
        assert _humanize(125) == "2 分 5 秒"

    def test_hours(self) -> None:
        text = _humanize(3600 * 3 + 60 * 12)
        assert "3 小时" in text
        assert "12 分" in text

    def test_negative_clamped(self) -> None:
        assert _humanize(-5) == "0 秒"


# =============================================================================
# 配置来源统一：tasks.json 必须优先于环境变量
#
# 回归背景：reschedule() 之前用环境变量做首次排程，跑完一次后又从
# tasks.json 重读 —— 用户在工作台改了时间，重启后第一次跑还是老时间。
# =============================================================================


class TestScheduleSourceOfTruth:
    def test_schedule_from_config_reads_tasks_json(self, repo: Repository, tmp_path: Any) -> None:
        repo.save_schedule(
            Schedule(enabled=True, hour=21, minute=5, jitter_minutes=3)
        )

        # settings 必须指向这个临时仓库，否则读的是真实 data 目录
        settings = dataclasses.replace(load_settings(), data_dir=tmp_path / "data")

        class FakeScheduler:
            pass

        scheduler = FakeScheduler()
        scheduler.settings = settings
        scheduler._schedule_from_config = jobs_mod.Scheduler._schedule_from_config.__get__(scheduler)

        got = scheduler._schedule_from_config()
        assert (got.hour, got.minute) == (21, 5)

    def test_falls_back_to_settings_when_tasks_json_unreadable(self, repo: Repository) -> None:
        class FakeScheduler:
            def _schedule_from_config(self) -> Schedule:
                # 模拟「读 tasks.json 抛异常」
                raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            FakeScheduler()._schedule_from_config()

        # 正常路径（仓库可读但文件为空）应返回默认值而不是抛异常
        got = repo.load_schedule()
        assert isinstance(got, Schedule)


# =============================================================================
# run_once：执行编排（用替身引擎，不碰真浏览器）
# =============================================================================


@dataclasses.dataclass
class FakeSendOutcome:
    contact_name: str
    status: RunStatus
    detail: str = ""
    failure_kind: FailureKind | None = None
    elapsed_seconds: float = 0.1

    def to_target_outcome(self) -> TargetOutcome:
        return TargetOutcome(
            name=self.contact_name,
            status=self.status,
            detail=self.detail,
            failure_kind=self.failure_kind,
        )


class FakeEngine:
    """按预设脚本逐次返回发送结果。

    ``run_once`` 会自己创建引擎实例，所以预设脚本放在**类级队列**里，
    每个新实例按顺序取一份。
    """

    instances: ClassVar[list[FakeEngine]] = []
    scripts: ClassVar[list[list[FakeSendOutcome]]] = []

    def __init__(self, browser_settings: Any, send_settings: Any) -> None:
        self.script = FakeEngine.scripts.pop(0) if FakeEngine.scripts else []
        self.calls: list[tuple[str, str]] = []
        self.started = False
        self.stopped = False
        FakeEngine.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def load_state(self, path: Any) -> None:
        self.state_path = path

    def check_login(self) -> Any:
        return dataclasses.simple_namespace if False else _LoginCheck(ok=True, reason="OK", detail="ok")

    def send_to_contact(self, contact: Any, message: Any, *, dry_run: bool, allow_search: bool) -> FakeSendOutcome:
        self.calls.append((contact.name, message.content))
        outcome = self.script.pop(0) if self.script else FakeSendOutcome(
            contact_name=contact.name, status=RunStatus.SUCCESS, detail="ok"
        )
        return outcome


@dataclasses.dataclass
class _LoginCheck:
    ok: bool
    reason: str
    detail: str


class TestRunOnce:
    @pytest.fixture(autouse=True)
    def _patch_engine(self, monkeypatch: Any, tmp_path: Any, repo: Repository) -> None:
        FakeEngine.instances.clear()
        import douyin_huohua_keeper.engine as engine_pkg

        monkeypatch.setattr(engine_pkg, "Engine", FakeEngine)
        monkeypatch.setattr(
            "douyin_huohua_keeper.engine.auth.check_cookie_freshness",
            lambda path: types.SimpleNamespace(
                expired=False, needs_renewal=False, detail="ok"
            ),
        )
        # settings 指向临时仓库，避免读写真实 data 目录
        self.settings = dataclasses.replace(load_settings(), data_dir=repo.data_dir)

    def _prepare(self, repo: Repository, names: tuple[str, ...] = ("小明",)) -> None:
        for name in names:
            repo.upsert_contact(Contact(name=name))
        repo.save_tasks(
            {**repo.load_tasks(), "messages": [{"kind": "text", "content": "早"}]}
        )

    def test_success_marks_sent_and_closes_engine(self, repo: Repository) -> None:
        self._prepare(repo)
        settings = self.settings

        report = run_once(settings, repository=repo, dry_run=False)

        assert report is not None
        assert report.outcomes[0].status is RunStatus.SUCCESS
        engine = FakeEngine.instances[-1]
        assert engine.started and engine.stopped
        # 防重复标记已写入
        contacts = {c.name: c for c in repo.load_contacts()}
        assert contacts["小明"].last_sent_on is not None

    def test_dry_run_does_not_mark_sent(self, repo: Repository) -> None:
        self._prepare(repo)
        settings = self.settings

        report = run_once(settings, repository=repo, dry_run=True)

        assert report is not None
        assert report.dry_run is True
        contacts = {c.name: c for c in repo.load_contacts()}
        assert contacts["小明"].last_sent_on is None
        # 演练不落盘报告
        assert report.outcomes[0].detail

    def test_no_contacts_returns_none(self, repo: Repository) -> None:
        report = run_once(self.settings, repository=repo, dry_run=False)
        assert report is None

    def test_transient_failure_retries_then_succeeds(self, repo: Repository) -> None:
        self._prepare(repo)
        FakeEngine.scripts = [
            [
                FakeSendOutcome(
                    contact_name="小明",
                    status=RunStatus.FAILED,
                    detail="抖动",
                    failure_kind=FailureKind.TRANSIENT,
                ),
                FakeSendOutcome(contact_name="小明", status=RunStatus.SUCCESS),
            ]
        ]
        report = run_once(self.settings, repository=repo, dry_run=False)

        assert report is not None
        assert report.outcomes[0].status is RunStatus.SUCCESS
        assert len(FakeEngine.instances[-1].calls) == 2  # 重试了一次

    def test_auth_failure_aborts_entire_run(self, repo: Repository) -> None:
        self._prepare(repo, names=("小明", "小红"))
        FakeEngine.scripts = [
            [
                FakeSendOutcome(
                    contact_name="小明",
                    status=RunStatus.FAILED,
                    detail="登录失效",
                    failure_kind=FailureKind.AUTH,
                ),
            ]
        ]
        report = run_once(self.settings, repository=repo, dry_run=False)

        assert report is not None
        # AUTH 直接中止，第二个收件人不会被尝试
        assert len(FakeEngine.instances[-1].calls) == 1
        assert report.outcomes[0].failure_kind is FailureKind.AUTH

    def test_interval_gap_applied_between_contacts(self, repo: Repository) -> None:
        self._prepare(repo, names=("小明", "小红"))
        repo.save_tasks({**repo.load_tasks(), "interval": {"minimum": 0.0, "maximum": 0.0}})
        settings = self.settings
        report = run_once(settings, repository=repo, dry_run=False)

        assert report is not None
        assert len(report.outcomes) == 2

    # --- ★ 用户硬性要求：手动点一次就真发一次 --------------------------------

    def _prepare_already_sent(self, repo: Repository) -> None:
        repo.upsert_contact(Contact(name="小明", last_sent_on=today_str()))
        repo.save_tasks({**repo.load_tasks(), "messages": [{"kind": "text", "content": "早"}]})

    def test_manual_run_sends_even_if_already_sent_today(self, repo: Repository) -> None:
        """★ 手动发送必须**真的发出去**，即使今天已经发过。

        回归背景：防重复的依据曾经是「有没有传 messages_override」，
        于是工作台首页的「立即发送一次」按钮（不传 message）被当成定时任务，
        点下去只得到一条 SKIPPED —— 用户以为发了，实际一条都没出去。
        """
        self._prepare_already_sent(repo)

        report = run_once(self.settings, repository=repo, dry_run=False, kind="manual")

        assert report is not None
        assert report.outcomes[0].status is RunStatus.SUCCESS
        assert len(FakeEngine.instances[-1].calls) == 1, "手动发送必须真的调用发送"

    def test_explicit_force_sends_even_when_dedupe_requested(self, repo: Repository) -> None:
        """路由层显式传 prevent_duplicates=False 时同样要真发。"""
        self._prepare_already_sent(repo)

        report = run_once(
            self.settings,
            repository=repo,
            dry_run=False,
            kind="manual",
            prevent_duplicates=False,
        )

        assert report is not None
        assert report.outcomes[0].status is RunStatus.SUCCESS

    def test_scheduled_run_skips_already_sent_today(self, repo: Repository) -> None:
        """定时任务仍然防重复 —— 这是防重复的主战场，不能被一起放开。"""
        self._prepare_already_sent(repo)

        report = run_once(self.settings, repository=repo, dry_run=False, kind="scheduled")

        assert report is not None
        assert report.outcomes[0].status is RunStatus.SKIPPED
        assert FakeEngine.instances[-1].calls == []


# =============================================================================
# 每日汇总（23:30）：时刻计算与文案
# =============================================================================


class TestDailyDigest:
    def test_next_daily_trigger_is_in_the_future(self) -> None:
        nxt = _next_daily_trigger(23, 30, TZ)

        assert nxt > datetime.now(TZ)
        assert (nxt.hour, nxt.minute) == (23, 30)

    def test_next_daily_trigger_rolls_over(self) -> None:
        """已过去的时刻必须顺延到明天，否则 job 会立刻触发。"""
        nxt = _next_daily_trigger(0, 0, TZ)

        assert nxt > datetime.now(TZ)

    def _capture_alert(self, monkeypatch: Any, entry: dict[str, Any]) -> Any:
        from douyin_huohua_keeper import notify as notify_pkg

        captured: dict[str, Any] = {}

        class FakeDispatcher:
            @classmethod
            def from_settings(cls, settings: Any) -> FakeDispatcher:
                return cls()

            def dispatch(self, alert: Any) -> Any:
                captured["alert"] = alert
                return types.SimpleNamespace(summary=lambda: "ok")

        monkeypatch.setattr(notify_pkg, "Dispatcher", FakeDispatcher)
        settings = load_settings()
        jobs_mod._send_daily_digest(settings, _FakeDigestRepo(entry))
        return captured["alert"]

    def test_digest_reports_failed_day(self, monkeypatch: Any) -> None:
        alert = self._capture_alert(
            monkeypatch,
            {"success": False, "attempts": 2, "last_summary": "成功 0/3，失败 3"},
        )

        assert alert.level is AlertLevel.NOTICE
        assert "没有成功" in alert.title
        assert "立即发送一次" in alert.body

    def test_digest_reports_successful_day(self, monkeypatch: Any) -> None:
        alert = self._capture_alert(
            monkeypatch,
            {
                "success": True,
                "attempts": 1,
                "sent_to": ["小明"],
                "last_summary": "成功 1/1，失败 0",
            },
        )

        assert "已续上" in alert.title
        assert "小明" in alert.body


class _FakeDigestRepo:
    """``_send_daily_digest`` 只用到这两个方法。"""

    def __init__(self, entry: dict[str, Any]) -> None:
        self._entry = entry

    def daily_entry(self, on: str | None = None) -> dict[str, Any]:
        return self._entry

    def load_schedule(self) -> Schedule:
        return Schedule(enabled=True, hour=9, minute=30, jitter_minutes=0)


# =============================================================================
# 「今天被静默跳过」的兜底提醒
#
# 回归场景：服务器在 09:30 那一刻没在运行（宕机/重启/断电）。
# 重启后 reschedule() 把下一次排到**明天** —— 今天就被静默跳过了：
# 没有报告、没有通知，用户以为发过了。对「火花不能断」的任务不能接受。
# =============================================================================


class TestMissedDayWarning:
    class _Repo:
        """只需要 ``config_dir`` 和 ``today_succeeded()``。"""

        def __init__(self, config_dir: Any, *, succeeded: bool) -> None:
            self.config_dir = config_dir
            self._succeeded = succeeded

        def today_succeeded(self) -> bool:
            return self._succeeded

    def _patch(self, monkeypatch: pytest.MonkeyPatch) -> list[Any]:
        from douyin_huohua_keeper import notify as notify_pkg

        sent: list[Any] = []

        class FakeDispatcher:
            @classmethod
            def from_settings(cls, settings: Any) -> FakeDispatcher:
                return cls()

            def dispatch(self, alert: Any) -> Any:
                sent.append(alert)
                return types.SimpleNamespace(summary=lambda: "ok")

        monkeypatch.setattr(notify_pkg, "Dispatcher", FakeDispatcher)
        return sent

    def test_warns_when_today_was_missed(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent = self._patch(monkeypatch)
        # 00:00 出发 —— 今天肯定已经过了（+15 分钟宽限）
        schedule = Schedule(enabled=True, hour=0, minute=0, jitter_minutes=0)
        repo = self._Repo(tmp_path, succeeded=False)

        jobs_mod._warn_if_today_missed(schedule, repo=repo)

        assert len(sent) == 1
        assert "没有成功执行" in sent[0].body

    def test_does_not_repeat_within_the_same_day(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """反复重启不能反复打扰。"""
        sent = self._patch(monkeypatch)
        schedule = Schedule(enabled=True, hour=0, minute=0, jitter_minutes=0)
        repo = self._Repo(tmp_path, succeeded=False)

        jobs_mod._warn_if_today_missed(schedule, repo=repo)
        jobs_mod._warn_if_today_missed(schedule, repo=repo)

        assert len(sent) == 1

    def test_silent_when_today_already_succeeded(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent = self._patch(monkeypatch)
        schedule = Schedule(enabled=True, hour=0, minute=0, jitter_minutes=0)
        repo = self._Repo(tmp_path, succeeded=True)

        jobs_mod._warn_if_today_missed(schedule, repo=repo)

        assert sent == []

    def test_silent_before_todays_time(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """今天还没到点（或刚到点 15 分钟内）时不该报警。"""
        sent = self._patch(monkeypatch)
        schedule = Schedule(enabled=True, hour=23, minute=59, jitter_minutes=0)
        repo = self._Repo(tmp_path, succeeded=False)

        jobs_mod._warn_if_today_missed(schedule, repo=repo)

        assert sent == []

    def test_silent_when_schedule_disabled(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent = self._patch(monkeypatch)
        schedule = Schedule(enabled=False, hour=0, minute=0, jitter_minutes=0)
        repo = self._Repo(tmp_path, succeeded=False)

        jobs_mod._warn_if_today_missed(schedule, repo=repo)

        assert sent == []
