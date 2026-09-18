"""文件锁测试。

锁的价值在于「防止两件事同时发生」，而这类 bug 往往在真实使用几个月后
才因为一次巧合暴露。所以要在这里测透。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from douyin_huohua_keeper.store.lock import (
    FileLock,
    LockBusyError,
    read_lock_holder,
)


class TestBasicLocking:
    def test_acquire_creates_lock_file(self, tmp_path: Path) -> None:
        lock_path = tmp_path / ".run.lock"
        lock = FileLock(lock_path)

        lock.acquire()
        try:
            assert lock_path.exists()
            assert lock.held
        finally:
            lock.release()

    def test_release_removes_lock_file(self, tmp_path: Path) -> None:
        lock_path = tmp_path / ".run.lock"
        lock = FileLock(lock_path)

        lock.acquire()
        lock.release()

        assert not lock_path.exists()
        assert not lock.held

    def test_context_manager(self, tmp_path: Path) -> None:
        lock_path = tmp_path / ".run.lock"

        with FileLock(lock_path) as lock:
            assert lock.held
            assert lock_path.exists()

        assert not lock_path.exists()

    def test_context_manager_releases_on_exception(self, tmp_path: Path) -> None:
        """抛异常也必须释放 —— 否则一次报错就永久锁死。"""
        lock_path = tmp_path / ".run.lock"

        with pytest.raises(RuntimeError), FileLock(lock_path):
            raise RuntimeError("任务炸了")

        assert not lock_path.exists()

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "deep" / "nested" / ".run.lock"
        with FileLock(lock_path):
            assert lock_path.exists()


class TestMutualExclusion:
    def test_second_acquire_is_rejected(self, tmp_path: Path) -> None:
        lock_path = tmp_path / ".run.lock"

        with FileLock(lock_path) as first, pytest.raises(LockBusyError):
            second = FileLock(lock_path)
            second.acquire()
            assert first.held  # 第一个仍然持有

    def test_can_reacquire_after_release(self, tmp_path: Path) -> None:
        lock_path = tmp_path / ".run.lock"

        with FileLock(lock_path):
            pass

        # 上一个释放后应该能重新获取
        with FileLock(lock_path) as lock:
            assert lock.held

    def test_double_acquire_on_same_object_raises(self, tmp_path: Path) -> None:
        lock = FileLock(tmp_path / ".run.lock")
        lock.acquire()
        try:
            with pytest.raises(RuntimeError, match="重复"):
                lock.acquire()
        finally:
            lock.release()

    def test_release_is_idempotent(self, tmp_path: Path) -> None:
        lock = FileLock(tmp_path / ".run.lock")
        lock.acquire()
        lock.release()
        lock.release()  # 不该抛异常

        assert not lock.held


class TestLockMetadata:
    def test_records_pid_and_timestamp(self, tmp_path: Path) -> None:
        lock_path = tmp_path / ".run.lock"

        with FileLock(lock_path):
            holder = read_lock_holder(lock_path)
            assert holder is not None
            assert holder["pid"] == os.getpid()
            assert "acquired_at" in holder
            assert "acquired_ts" in holder

    def test_busy_error_names_the_holder(self, tmp_path: Path) -> None:
        """报错要能告诉用户「谁占着」，否则排查无从下手。"""
        lock_path = tmp_path / ".run.lock"

        with FileLock(lock_path), pytest.raises(LockBusyError) as exc_info:
            FileLock(lock_path).acquire()

        error = exc_info.value
        assert error.holder.get("pid") == os.getpid()
        assert "pid=" in str(error)


class TestStaleLockRecovery:
    def test_recovers_from_very_old_lock(self, tmp_path: Path) -> None:
        """进程崩溃留下的锁不该导致永久锁死。

        这是「一次意外之后再也跑不起来」的那类恶心 bug，
        自动恢复比让用户手动删文件友好得多。
        """
        lock_path = tmp_path / ".run.lock"
        lock_path.write_text(
            json.dumps({"pid": 999_999, "acquired_at": "2020-01-01T00:00:00", "acquired_ts": 1.0}),
            encoding="utf-8",
        )
        # 把文件时间也改老
        old = time.time() - 99999
        os.utime(lock_path, (old, old))

        lock = FileLock(lock_path, stale_seconds=60.0)
        lock.acquire()
        try:
            assert lock.held
            holder = read_lock_holder(lock_path)
            assert holder["pid"] == os.getpid()
        finally:
            lock.release()

    def test_dead_pid_is_treated_as_stale(self, tmp_path: Path) -> None:
        """POSIX 上 pid 不存在就该认为是残留锁。

        Windows 上退化为只按时间判断，所以这个用例只在非 Windows 生效。
        """
        if os.name == "nt":
            pytest.skip("Windows 上不做 pid 存活检查")

        lock_path = tmp_path / ".run.lock"
        lock_path.write_text(
            json.dumps({"pid": 999_999_999, "acquired_ts": time.time()}),
            encoding="utf-8",
        )

        lock = FileLock(lock_path)
        lock.acquire()
        try:
            assert lock.held
        finally:
            lock.release()

    def test_fresh_lock_is_not_stolen(self, tmp_path: Path) -> None:
        """新鲜锁绝不能被抢走 —— 那等于锁根本不存在。"""
        lock_path = tmp_path / ".run.lock"
        lock_path.write_text(
            json.dumps({"pid": os.getpid(), "acquired_ts": time.time()}),
            encoding="utf-8",
        )

        lock = FileLock(lock_path, stale_seconds=3600.0)
        with pytest.raises(LockBusyError):
            lock.acquire()

    def test_empty_lock_file_is_recoverable_when_old(self, tmp_path: Path) -> None:
        """内容读不出来的锁文件（创建时崩了），够老就当残留。"""
        lock_path = tmp_path / ".run.lock"
        lock_path.write_text("", encoding="utf-8")
        old = time.time() - 99999
        os.utime(lock_path, (old, old))

        lock = FileLock(lock_path, stale_seconds=60.0)
        lock.acquire()
        try:
            assert lock.held
        finally:
            lock.release()


class TestAgeTracking:
    def test_age_is_none_when_not_held(self, tmp_path: Path) -> None:
        assert FileLock(tmp_path / ".run.lock").age_seconds() is None

    def test_age_increases_while_held(self, tmp_path: Path) -> None:
        with FileLock(tmp_path / ".run.lock") as lock:
            first = lock.age_seconds()
            assert first is not None
            time.sleep(0.05)
            second = lock.age_seconds()
            assert second is not None
            assert second > first
