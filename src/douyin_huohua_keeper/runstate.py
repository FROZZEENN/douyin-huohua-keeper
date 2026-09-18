"""运行进度（实时状态）单例。

工作台首页要展示「任务进行状态」——正在给谁发、第几个了。但发送主链路
（``scheduler/runner.py`` 的 ``run_once``）本身不暴露中间状态，只有跑完才出报告。

这个模块用一个**进程级**单例记录当前这一次运行的进度：

- 单进程服务（Docker 里一个 uvicorn 进程）下，模块级变量天然全局可见；
- APScheduler ``max_instances=1`` + 文件锁保证同一时刻只有一个任务在跑，
  所以一个进度槽就够；
- **所有写操作都内部吞异常**——进度展示是「锦上添花」，绝不能因为这里出错
  而打断真正的发送。任何异常都会被静默丢掉，发送照常进行。

前端通过 ``/api/system/run-progress`` 轮询这个快照。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from .store import now_str


@dataclass
class _Progress:
    active: bool = False
    kind: str | None = None  # "scheduled" / "manual" / "broadcast"
    dry_run: bool = False
    started_at: str | None = None
    run_id: str | None = None
    current_target: str | None = None
    stage: str | None = None
    total: int = 0
    current_index: int = 0  # 正在处理的第几个（1-based；0 表示还没开始）
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    uncertain: int = 0
    targets: list[str] = field(default_factory=list)


_state = _Progress()
_lock = threading.Lock()


def snapshot() -> dict[str, Any]:
    """当前进度的不可变快照，直接序列化给前端。"""
    try:
        with _lock:
            return {
                "active": _state.active,
                "kind": _state.kind,
                "dry_run": _state.dry_run,
                "started_at": _state.started_at,
                "run_id": _state.run_id,
                "current_target": _state.current_target,
                "stage": _state.stage,
                "total": _state.total,
                "current_index": _state.current_index,
                "sent": _state.sent,
                "failed": _state.failed,
                "skipped": _state.skipped,
                "uncertain": _state.uncertain,
                "targets": list(_state.targets),
            }
    except Exception:  # noqa: BLE001
        return {"active": False}


def begin(
    *,
    kind: str,
    dry_run: bool,
    run_id: str | None,
    targets: list[str],
) -> None:
    """一次运行开始：记录任务来源、目标名单、总数。"""
    try:
        with _lock:
            _state.active = True
            _state.kind = kind
            _state.dry_run = dry_run
            _state.started_at = now_str()
            _state.run_id = run_id
            _state.current_target = None
            _state.stage = "准备中"
            _state.total = len(targets)
            _state.current_index = 0
            _state.sent = _state.failed = _state.skipped = _state.uncertain = 0
            _state.targets = list(targets)
    except Exception:  # noqa: BLE001
        pass


def start_target(name: str, position: int) -> None:
    """开始给第 ``position`` 个（1-based）收件人发送。"""
    try:
        with _lock:
            if not _state.active:
                return
            _state.current_index = position
            _state.current_target = name
            _state.stage = f"正在给 {name} 发送（第 {position}/{_state.total} 个）"
    except Exception:  # noqa: BLE001
        pass


def set_stage(stage: str) -> None:
    """更新一句话状态（例如错峰等待、中止原因）。"""
    try:
        with _lock:
            if _state.active:
                _state.stage = stage
    except Exception:  # noqa: BLE001
        pass


def mark_outcome(status_value: str) -> None:
    """一个收件人处理完：累加计数并推进进度。"""
    try:
        with _lock:
            if status_value == "success":
                _state.sent += 1
            elif status_value == "failed":
                _state.failed += 1
            elif status_value == "skipped":
                _state.skipped += 1
            elif status_value == "uncertain":
                _state.uncertain += 1
            _state.current_target = None
            if _state.current_index < _state.total:
                _state.stage = f"已完成 {_state.current_index}/{_state.total}"
    except Exception:  # noqa: BLE001
        pass


def finish() -> None:
    """运行结束：标记为非活动。"""
    try:
        with _lock:
            _state.active = False
            _state.current_target = None
            if _state.stage and _state.stage.startswith("正在"):
                _state.stage = "运行结束"
    except Exception:  # noqa: BLE001
        pass


__all__ = ["begin", "finish", "mark_outcome", "set_stage", "snapshot", "start_target"]
