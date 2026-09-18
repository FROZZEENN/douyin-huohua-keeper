"""存储层：原子写、文件锁、运行历史。

三件事，都很小，但都容不得含糊：

- ``atomic``  —— 先写 ``*.tmp`` 再 ``os.replace``。断电、被 kill、磁盘写满，
                都只会留下一个废弃的临时文件，不会让主文件变成半截 JSON
- ``lock``    —— 基于 ``O_CREAT | O_EXCL`` 的单实例锁。防止定时任务和手工
                触发同时跑，把登录态和运行历史搅成一团
- ``repo``    —— 账号、配置、运行历史的读写门面，统一走 atomic + lock

选 JSON 而不是 SQLite 是刻意的：个人自用场景下数据量极小，「少一个依赖」
的价值高于「事务和索引」。代价是并发写要靠文件锁自己兜，所以锁要写对。
"""

from __future__ import annotations

from .atomic import CorruptFileError, atomic_write_json, atomic_write_text, read_json
from .lock import FileLock, LockBusyError, read_lock_holder
from .repo import (
    SCHEMA_VERSION,
    Repository,
    new_run_id,
    now_str,
    repository_for,
    today_str,
    today_succeeded,
)

__all__ = [
    "SCHEMA_VERSION",
    "CorruptFileError",
    "FileLock",
    "LockBusyError",
    "Repository",
    "atomic_write_json",
    "atomic_write_text",
    "new_run_id",
    "now_str",
    "read_json",
    "read_lock_holder",
    "repository_for",
    "today_str",
    "today_succeeded",
]
