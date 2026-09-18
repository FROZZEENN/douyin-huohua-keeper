"""数据仓库测试。

重点验证三件事：
1. 首次启动（什么都没有）能正常工作，不会崩
2. 写入 → 读取 往返正确
3. 连续成功 / 失败计数在同一天重复时不会重复累加（否则手动重试就会误报）
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from douyin_huohua_keeper.models import (
    Contact,
    RunReport,
    RunStatus,
    Schedule,
    SendInterval,
    TargetOutcome,
)
from douyin_huohua_keeper.store.repo import Repository, new_run_id, today_str


@pytest.fixture
def repo(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "data")
    repository.ensure_layout()
    return repository


class TestLayout:
    def test_ensure_layout_creates_directories(self, tmp_path: Path) -> None:
        repository = Repository(tmp_path / "fresh")
        repository.ensure_layout()

        assert repository.accounts_dir.is_dir()
        assert repository.config_dir.is_dir()
        assert repository.runs_dir.is_dir()

    def test_ensure_layout_is_idempotent(self, repo: Repository) -> None:
        repo.ensure_layout()
        repo.ensure_layout()


class TestAccountState:
    def test_missing_state_returns_none(self, repo: Repository) -> None:
        assert repo.load_account_state("main") is None
        assert not repo.has_account_state("main")

    def test_roundtrip(self, repo: Repository) -> None:
        state = {"cookies": [{"name": "sessionid", "value": "secret"}], "origins": []}
        repo.save_account_state("main", state)

        assert repo.load_account_state("main") == state
        assert repo.has_account_state("main")

    def test_list_accounts(self, repo: Repository) -> None:
        repo.save_account_state("primary", {"cookies": []})
        repo.save_account_state("backup", {"cookies": []})

        assert repo.list_accounts() == ("backup", "primary")

    def test_delete_state(self, repo: Repository) -> None:
        repo.save_account_state("main", {"cookies": []})

        assert repo.delete_account_state("main") is True
        assert repo.delete_account_state("main") is False

    def test_age_tracking(self, repo: Repository) -> None:
        assert repo.account_state_age_days("main") is None

        repo.save_account_state("main", {"cookies": []})
        age = repo.account_state_age_days("main")
        assert age is not None
        assert age < 1.0

    def test_state_file_permissions_are_tight(self, repo: Repository) -> None:
        """登录态等同密码，POSIX 上必须是 600。"""
        import os

        if os.name == "nt":
            pytest.skip("Windows 上依赖目录 ACL")

        path = repo.save_account_state("main", {"cookies": []})
        mode = path.stat().st_mode & 0o777
        assert mode == 0o600, f"权限过宽：{oct(mode)}"


class TestContacts:
    def test_empty_by_default(self, repo: Repository) -> None:
        assert repo.load_contacts() == ()

    def test_save_and_load(self, repo: Repository) -> None:
        contacts = (
            Contact(name="小明", note="同学"),
            Contact(name="小红", enabled=False),
        )
        repo.save_contacts(contacts)

        loaded = repo.load_contacts()
        assert len(loaded) == 2
        assert loaded[0].name == "小明"
        assert loaded[0].note == "同学"
        assert loaded[1].enabled is False

    def test_enabled_filter(self, repo: Repository) -> None:
        repo.save_contacts((Contact(name="A"), Contact(name="B", enabled=False)))

        enabled = repo.enabled_contacts()
        assert [c.name for c in enabled] == ["A"]

    def test_upsert_inserts_new(self, repo: Repository) -> None:
        repo.upsert_contact(Contact(name="小明"))

        assert [c.name for c in repo.load_contacts()] == ["小明"]

    def test_upsert_updates_existing(self, repo: Repository) -> None:
        """按名字匹配更新，不该产生重复条目。"""
        repo.upsert_contact(Contact(name="小明", note="旧备注"))
        repo.upsert_contact(Contact(name="小明", note="新备注"))

        loaded = repo.load_contacts()
        assert len(loaded) == 1
        assert loaded[0].note == "新备注"

    def test_remove(self, repo: Repository) -> None:
        repo.save_contacts((Contact(name="A"), Contact(name="B")))

        remaining = repo.remove_contact("A")

        assert [c.name for c in remaining] == ["B"]

    def test_mark_sent_today(self, repo: Repository) -> None:
        repo.save_contacts((Contact(name="A"), Contact(name="B")))

        repo.mark_sent_today(("A",))

        loaded = {c.name: c for c in repo.load_contacts()}
        assert loaded["A"].last_sent_on == today_str()
        assert loaded["B"].last_sent_on is None

    def test_sent_today_helper(self, repo: Repository) -> None:
        repo.save_contacts((Contact(name="A"),))
        repo.mark_sent_today(("A",))

        contact = repo.load_contacts()[0]
        assert contact.sent_today(today_str()) is True
        assert contact.sent_today("2020-01-01") is False


class TestTasks:
    def test_defaults_when_missing(self, repo: Repository) -> None:
        tasks = repo.load_tasks()

        assert tasks["schedule"]["hour"] == 10
        assert tasks["schedule"]["minute"] == 30
        assert tasks["schedule"]["jitter_minutes"] == 25
        assert tasks["version"] == 1

    def test_schedule_roundtrip(self, repo: Repository) -> None:
        repo.save_schedule(Schedule(enabled=True, hour=8, minute=15, jitter_minutes=10))

        schedule = repo.load_schedule()
        assert schedule.hour == 8
        assert schedule.minute == 15
        assert schedule.jitter_minutes == 10

    def test_schedule_defaults_when_missing(self, repo: Repository) -> None:
        schedule = repo.load_schedule()
        assert schedule.hour == 10
        assert schedule.jitter_minutes == 25

    def test_messages_accept_plain_strings(self, repo: Repository) -> None:
        """消息池里直接写字符串是最自然的写法，要支持。"""
        repo.save_tasks({"messages": ["早", "在忙吗", "今天怎么样"]})

        messages = repo.load_messages()
        assert len(messages) == 3
        assert all(m.kind == "text" for m in messages)
        assert messages[0].content == "早"

    def test_messages_skip_invalid_entries(self, repo: Repository) -> None:
        """单条配置有问题不该让整个任务起不来，但会被跳过。"""
        repo.save_tasks(
            {
                "messages": [
                    "正常的一条",
                    {"kind": "text"},  # 缺 content，校验会失败
                    {"kind": "image"},  # 缺 path
                    "另一条正常的",
                ]
            }
        )

        messages = repo.load_messages()
        assert [m.content for m in messages] == ["正常的一条", "另一条正常的"]

    def test_interval_roundtrip(self, repo: Repository) -> None:
        repo.save_tasks({"interval": {"minimum": 5.0, "maximum": 12.0}})

        interval = repo.load_interval()
        assert interval.minimum == 5.0
        assert interval.maximum == 12.0

    def test_interval_defaults(self, repo: Repository) -> None:
        interval = repo.load_interval()
        assert isinstance(interval, SendInterval)
        assert interval.minimum == 3.0


class TestStreakState:
    def test_initial_state(self, repo: Repository) -> None:
        state = repo.load_streak_state()

        assert state["consecutive_failures"] == 0
        assert state["consecutive_successes"] == 0

    def test_success_increments_and_resets_failures(self, repo: Repository) -> None:
        repo.record_failure()
        repo.record_failure()  # 同一天，只算一次

        state = repo.record_success()
        assert state["consecutive_failures"] == 0
        assert state["consecutive_successes"] == 1

    def test_repeated_success_same_day_does_not_double_count(self, repo: Repository) -> None:
        """手动重试成功三次不该变成「连续成功 3 天」。"""
        repo.record_success()
        repo.record_success()
        state = repo.record_success()

        assert state["consecutive_successes"] == 1

    def test_repeated_failure_same_day_does_not_double_count(self, repo: Repository) -> None:
        """关键：否则手动重试三次就直接跳到 CRITICAL 告警了。"""
        repo.record_failure()
        repo.record_failure()
        state = repo.record_failure()

        assert state["consecutive_failures"] == 1

    def test_streak_start_date_set_on_first_success(self, repo: Repository) -> None:
        state = repo.record_success()

        assert state["streak_start_date"] == today_str()

    def test_streak_start_date_not_overwritten(self, repo: Repository) -> None:
        repo.save_streak_state({"streak_start_date": "2024-01-01", "consecutive_successes": 500})

        state = repo.record_success()
        assert state["streak_start_date"] == "2024-01-01"

    def test_success_resets_alert_level(self, repo: Repository) -> None:
        state = repo.load_streak_state()
        state["last_alert_level"] = "critical"
        repo.save_streak_state(state)

        assert repo.record_success()["last_alert_level"] is None


class TestRunHistory:
    def _report(self, *, success: bool, run_id: str | None = None) -> RunReport:
        outcome = TargetOutcome(
            name="小明",
            status=RunStatus.SUCCESS if success else RunStatus.FAILED,
            sent=1 if success else 0,
            detail=None if success else "找不到会话",
        )
        return RunReport(
            run_id=run_id or new_run_id(),
            account_id="main",
            started_at="2026-09-14T10:30:00",
            finished_at="2026-09-14T10:30:12",
            dry_run=False,
            outcomes=(outcome,),
        )

    def test_save_creates_report_file(self, repo: Repository) -> None:
        report = self._report(success=True)

        path = repo.save_report(report)

        assert path.is_file()
        assert repo.load_report(report.run_id)["run_id"] == report.run_id

    def test_save_updates_daily_index(self, repo: Repository) -> None:
        report = self._report(success=True)
        repo.save_report(report)

        entry = repo.daily_entry(report.finished_at[:10])
        assert entry is not None
        assert entry["success"] is True
        assert entry["sent_to"] == ["小明"]

    def test_failure_does_not_mark_day_successful(self, repo: Repository) -> None:
        repo.save_report(self._report(success=False))

        assert repo.today_succeeded(_today_of_report()) is False

    def test_success_marks_day_successful(self, repo: Repository) -> None:
        repo.save_report(self._report(success=True))

        assert repo.today_succeeded(_today_of_report()) is True

    def test_later_failure_does_not_erase_earlier_success(self, repo: Repository) -> None:
        """已经成功过就该保持成功 —— 否则一次失败的补发会让当日状态倒退。"""
        repo.save_report(self._report(success=True))
        repo.save_report(self._report(success=False))

        assert repo.today_succeeded(_today_of_report()) is True

    def test_attempts_counter(self, repo: Repository) -> None:
        repo.save_report(self._report(success=False))
        repo.save_report(self._report(success=True))

        entry = repo.daily_entry(_today_of_report())
        assert entry["attempts"] == 2

    def test_recent_days_sorted_newest_first(self, repo: Repository) -> None:
        for day in ("2026-09-11", "2026-09-13", "2026-09-12"):
            report = self._report(success=True)
            report = RunReport(
                run_id=report.run_id,
                account_id=report.account_id,
                started_at=f"{day}T10:30:00",
                finished_at=f"{day}T10:30:12",
                dry_run=False,
                outcomes=report.outcomes,
            )
            repo.save_report(report)

        days = [day for day, _ in repo.recent_days()]
        assert days == ["2026-09-13", "2026-09-12", "2026-09-11"]

    def test_recent_days_respects_limit(self, repo: Repository) -> None:
        for offset in range(5):
            day = (date(2026, 9, 1) + timedelta(days=offset)).isoformat()
            base = self._report(success=True)
            repo.save_report(
                RunReport(
                    run_id=base.run_id,
                    account_id="main",
                    started_at=f"{day}T10:00:00",
                    finished_at=f"{day}T10:00:10",
                    dry_run=False,
                    outcomes=base.outcomes,
                )
            )

        assert len(repo.recent_days(limit=3)) == 3

    def test_daily_entry_missing_returns_none(self, repo: Repository) -> None:
        assert repo.daily_entry("1999-01-01") is None

    def test_list_run_ids(self, repo: Repository) -> None:
        ids = [repo.save_report(self._report(success=True)).parent.name for _ in range(3)]

        listed = repo.list_run_ids()
        assert sorted(listed) == sorted(ids)

    def test_corrupt_daily_index_is_not_fatal(self, repo: Repository) -> None:
        """索引坏了最多是「不知道今天成功没」，不该让服务起不来。"""
        repo.daily_index_path.write_text("{ broken", encoding="utf-8")

        # 键值对读取会抛 CorruptFileError，但调用方（工作台）会兜住
        from douyin_huohua_keeper.store.atomic import CorruptFileError

        with pytest.raises(CorruptFileError):
            repo.today_succeeded("2026-09-14")


class TestRunLock:
    def test_run_lock_is_exclusive(self, repo: Repository) -> None:
        from douyin_huohua_keeper.store.lock import LockBusyError

        with repo.run_lock(), pytest.raises(LockBusyError):
            repo.run_lock().acquire()

    def test_cleanup_removes_stale_temp(self, repo: Repository) -> None:
        """启动时的 cleanup 只清「够老」的临时文件。

        刚写出来的临时文件可能是另一个进程正在用的 —— 所以默认保留 1 小时。
        这里把时间改老来模拟真正的残留。
        """
        import os
        import time

        stale = repo.config_dir / ".contacts.json.tmp"
        stale.write_text("leftover")
        old = time.time() - 7200
        os.utime(stale, (old, old))

        removed = repo.cleanup()

        assert removed == 1
        assert not stale.exists()

    def test_cleanup_keeps_fresh_temp(self, repo: Repository) -> None:
        """不能误删正在写入的临时文件。"""
        fresh = repo.config_dir / ".contacts.json.tmp"
        fresh.write_text("in progress")

        removed = repo.cleanup()

        assert removed == 0
        assert fresh.exists()


def _today_of_report() -> str:
    """报告里的完成时间是硬编码的 2026-09-14，测试要按它判断。"""
    return "2026-09-14"
