"""原子写测试。

这里的每个用例都对应一种真实的损坏场景。跑得快，但覆盖的是最不能出错的部分。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from douyin_huohua_keeper.store.atomic import (
    CorruptFileError,
    atomic_write_json,
    atomic_write_text,
    cleanup_stale_temp_files,
    read_json,
)


class TestAtomicWriteText:
    def test_writes_content(self, tmp_path: Path) -> None:
        target = tmp_path / "hello.txt"
        atomic_write_text(target, "你好")

        assert target.read_text(encoding="utf-8") == "你好"

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b" / "c" / "deep.txt"
        atomic_write_text(target, "ok")

        assert target.is_file()

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        target = tmp_path / "file.txt"
        atomic_write_text(target, "first")
        atomic_write_text(target, "second")

        assert target.read_text(encoding="utf-8") == "second"

    def test_leaves_no_temp_files_behind(self, tmp_path: Path) -> None:
        target = tmp_path / "file.txt"
        atomic_write_text(target, "content")

        leftovers = [p for p in tmp_path.iterdir() if p.name != "file.txt"]
        assert leftovers == [], f"残留了临时文件：{leftovers}"

    def test_no_intermediate_state_visible(self, tmp_path: Path, monkeypatch) -> None:
        """核心保证：目标文件要么不存在，要么是完整的。

        模拟「写到一半崩溃」—— 让 os.replace 之前失败，
        目标文件必须保持原样（这里是从「不存在」保持为「不存在」）。
        """
        target = tmp_path / "important.json"
        original_replace = os.replace

        def boom(*args, **kwargs):
            raise OSError("模拟磁盘故障")

        monkeypatch.setattr(os, "replace", boom)

        with pytest.raises(OSError):
            atomic_write_text(target, '{"partial": true')

        monkeypatch.setattr(os, "replace", original_replace)

        # 目标文件不该被创建
        assert not target.exists(), "replace 失败却创建了目标文件"
        # 临时文件应该被清理掉
        leftovers = [p for p in tmp_path.iterdir() if p.name != "important.json"]
        assert leftovers == [], f"失败后残留了临时文件：{leftovers}"

    def test_preserves_existing_content_on_failure(self, tmp_path: Path, monkeypatch) -> None:
        """写入失败时，旧内容必须完好无损 —— 这正是原子写的价值。"""
        target = tmp_path / "config.json"
        atomic_write_json(target, {"version": 1, "data": "original"})

        def boom(*args, **kwargs):
            raise OSError("模拟磁盘故障")

        monkeypatch.setattr(os, "replace", boom)

        with pytest.raises(OSError):
            atomic_write_json(target, {"version": 2, "data": "new"})

        # 旧内容还在
        assert read_json(target) == {"version": 1, "data": "original"}


class TestAtomicWriteJson:
    def test_roundtrip(self, tmp_path: Path) -> None:
        target = tmp_path / "data.json"
        payload = {"name": "小明", "count": 42, "nested": {"a": [1, 2, 3]}}

        atomic_write_json(target, payload)

        assert read_json(target) == payload

    def test_keeps_chinese_readable(self, tmp_path: Path) -> None:
        """不转义中文 —— 否则人工排查配置文件时会很痛苦。"""
        target = tmp_path / "data.json"
        atomic_write_json(target, {"friend": "小红"})

        raw = target.read_text(encoding="utf-8")
        assert "小红" in raw
        assert "\\u5c0f\\u7ea2" not in raw

    def test_ends_with_newline(self, tmp_path: Path) -> None:
        target = tmp_path / "data.json"
        atomic_write_json(target, {"a": 1})

        assert target.read_text(encoding="utf-8").endswith("\n")

    def test_indented_for_human_reading(self, tmp_path: Path) -> None:
        target = tmp_path / "data.json"
        atomic_write_json(target, {"a": 1})

        assert "\n" in target.read_text(encoding="utf-8").strip()


class TestReadJson:
    def test_missing_file_returns_default(self, tmp_path: Path) -> None:
        assert read_json(tmp_path / "nope.json") is None
        assert read_json(tmp_path / "nope.json", default={}) == {}

    def test_corrupt_file_raises_not_silently_defaults(self, tmp_path: Path) -> None:
        """损坏必须显式报错。

        如果这里静默返回 default，用户会以为「配置还没写过」，
        联系人莫名其妙消失却找不到原因。
        """
        target = tmp_path / "broken.json"
        target.write_text('{"incomplete": ', encoding="utf-8")

        with pytest.raises(CorruptFileError) as exc_info:
            read_json(target, default={})

        assert "broken.json" in str(exc_info.value)

    def test_corrupt_error_mentions_recovery(self, tmp_path: Path) -> None:
        target = tmp_path / "broken.json"
        target.write_text("not json at all", encoding="utf-8")

        with pytest.raises(CorruptFileError) as exc_info:
            read_json(target)

        message = str(exc_info.value)
        assert "损坏" in message or "corrupt" in message.lower()


def _make_stale(path: Path, *, age_seconds: float = 3600.0) -> None:
    """把文件"变成旧的" —— 把 mtime 推到过去。

    ⚠️ 为什么必须显式改 mtime，而不靠「刚写完 + max_age_seconds=0」：

    **在 GitHub 的 Windows runner 上，这个写法时红时绿**（实测：CI 上
    ``test_recurses_into_subdirectories`` 报 ``assert 0 == 1``，本机连跑 30 次却全过）。
    最可能的原因是 Windows 的时间精度：``time.time()`` 只有 ~15.6ms 粒度且向下取整，
    而 NTFS 的文件时间戳是 100ns 精度 —— 刚写完的文件算出来 ``now - mtime`` 可能是
    **非正数**，于是被判定为"不陈旧"（也可能是 runner 上的杀软短暂占用导致 unlink 失败）。

    把 mtime 推到过去之后，「是否陈旧」与时间戳精度、时钟抖动都无关了 ——
    这才是修 flaky 用例的正确方式：**让它变确定，而不是让它更容易蒙对**。
    """
    when = time.time() - age_seconds
    os.utime(path, (when, when))


class TestCleanupStaleTempFiles:
    def test_removes_old_temp_files(self, tmp_path: Path) -> None:
        stale = tmp_path / ".data.json.abc123.tmp"
        stale.write_text("leftover")
        _make_stale(stale)

        removed = cleanup_stale_temp_files(tmp_path, max_age_seconds=0)

        assert removed == 1
        assert not stale.exists()

    def test_keeps_fresh_temp_files(self, tmp_path: Path) -> None:
        """正在写入的临时文件不能被误删。"""
        fresh = tmp_path / ".data.json.xyz789.tmp"
        fresh.write_text("in progress")

        removed = cleanup_stale_temp_files(tmp_path, max_age_seconds=3600)

        assert removed == 0
        assert fresh.exists()

    def test_recurses_into_subdirectories(self, tmp_path: Path) -> None:
        nested = tmp_path / "runs" / "2026-09-14"
        nested.mkdir(parents=True)
        stale = nested / ".report.json.tmp"
        stale.write_text("leftover")
        _make_stale(stale)  # 见 _make_stale 的注释：不改 mtime 会在 Windows 上 flaky

        removed = cleanup_stale_temp_files(tmp_path, max_age_seconds=0)

        assert removed == 1
        assert not stale.exists()

    def test_missing_directory_is_not_an_error(self, tmp_path: Path) -> None:
        assert cleanup_stale_temp_files(tmp_path / "nonexistent") == 0
