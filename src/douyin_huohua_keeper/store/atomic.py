"""原子写。

JSON 文件存储最大的风险不是「写错了」，而是**写到一半进程没了**。
这种情况留下的半截文件会让下次启动直接解析失败，而且内容不可恢复。

做法很朴素但有效：写临时文件 → ``fsync`` → ``os.replace`` 重命名。
``rename`` 在同一文件系统内是原子操作，所以目标文件要么是旧的完整版本，
要么是新的完整版本，不存在中间态。

Windows 上 ``os.replace`` 也是原子的（底层是 ``MoveFileEx`` 带
``MOVEFILE_REPLACE_EXISTING``），所以不需要平台分支。
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

# 临时文件后缀。用这个名字是为了让 .gitignore 里的 `*.tmp` 能兜住。
TMP_SUFFIX = ".tmp"


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """原子地写入文本。

    步骤：
    1. 在**同一目录**下建临时文件（跨目录 rename 不保证原子）
    2. 写完 flush + fsync，让数据真正落到磁盘
    3. ``os.replace`` 覆盖目标

    第 2 步容易被忽略但很关键：只 flush 不 fsync 的话，数据还在操作系统页缓存里，
    断电就没了 —— 而「断电」正是这个函数要防的场景之一。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=TMP_SUFFIX,
    )
    tmp_path = Path(tmp_name)

    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(tmp_path, path)

        # 目录项的元数据也要落盘，否则极端情况下可能 rename 丢失
        _sync_directory(path.parent)

    except BaseException:
        # 任何失败都要把临时文件清掉，不能留垃圾
        _unlink_quietly(tmp_path)
        raise


def atomic_write_json(path: Path, payload: Any, *, indent: int = 2) -> None:
    """原子地写入 JSON。

    ``ensure_ascii=False`` 是刻意的：配置文件和工作台里都是中文，
    转成 ``\\uXXXX`` 之后人工排查会很难受。
    """
    text = json.dumps(payload, ensure_ascii=False, indent=indent, sort_keys=False)
    if not text.endswith("\n"):
        text += "\n"
    atomic_write_text(path, text)


def read_json(path: Path, default: Any = None) -> Any:
    """读 JSON。

    文件不存在 → 返回 ``default``（首次启动的正常情况）。
    文件损坏 → 抛 :class:`CorruptFileError`，让调用方决定怎么处理。

    这里刻意**不**在解析失败时静默返回 default：那会把「文件坏了」伪装成
    「还没配置过」，用户会莫名其妙丢掉联系人列表还找不到原因。
    """
    path = Path(path)
    if not path.exists():
        return default

    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        # exists() 与 open() 之间文件被删了（并发）—— 仍然当作「还没有这个文件」。
        # 不管的话它会变成一个裸的 500。
        return default
    except UnicodeDecodeError as exc:
        # 不是合法 UTF-8：多半是写入中断留下的半截文件
        raise CorruptFileError(path, f"不是有效的 UTF-8 文本（{exc}）") from exc
    except json.JSONDecodeError as exc:
        raise CorruptFileError(path, str(exc)) from exc


class CorruptFileError(Exception):
    """文件存在但不是合法 JSON。"""

    def __init__(self, path: Path, detail: str) -> None:
        self.path = Path(path)
        self.detail = detail
        backup = self.path.with_suffix(self.path.suffix + ".corrupt")
        super().__init__(
            f"{self.path} 不是合法的 JSON：{detail}\n"
            f"该文件可能因写入中断而损坏。原文件已保留，"
            f"你可以手动检查，或重命名为 {backup.name} 后让程序重建。"
        )


def _sync_directory(directory: Path) -> None:
    """fsync 目录项。Windows 上不支持，直接跳过。"""
    if os.name == "nt":
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _unlink_quietly(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def cleanup_stale_temp_files(directory: Path, *, max_age_seconds: float = 3600.0) -> int:
    """清掉上次崩溃留下的临时文件。

    返回清理数量。只删超过 ``max_age_seconds`` 的，避免误删正在写入的文件。
    """
    import time

    directory = Path(directory)
    if not directory.is_dir():
        return 0

    now = time.time()
    removed = 0
    for candidate in directory.rglob(f"*{TMP_SUFFIX}"):
        # 只清理**我们自己**写的临时文件：``atomic_write_*`` 建的是
        # ``.<目标文件名>.<随机串>.tmp``（以点开头）。不加这个判断的话，
        # data/ 里任何其它 .tmp 也会被一起删掉。
        if not candidate.name.startswith("."):
            continue
        try:
            if now - candidate.stat().st_mtime > max_age_seconds:
                candidate.unlink()
                removed += 1
        except OSError:
            continue
    return removed
