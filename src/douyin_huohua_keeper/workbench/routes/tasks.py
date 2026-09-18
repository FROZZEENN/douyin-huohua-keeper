"""任务配置：发送时间、消息池、轮换策略、间隔。

**改完立刻生效**是关键体验：保存时间后马上调 ``Scheduler.reschedule``，
不需要重启服务。这也是把调度放在进程内而不是 cron 里的主要原因。

关于校验：所有写入都先构造 :class:`Schedule` / :class:`Message`，
靠它们自己的 ``__post_init__`` 兜住非法值。这样「什么算合法配置」只有
一处定义，不会出现「模型说不行但 API 接受了」的分裂。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ...models import Message, Schedule
from ...store.repo import now_str

LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

# 轮换策略的合法取值，与 repo._default_tasks 保持一致
ROTATIONS = ("all", "random", "round_robin")

ROTATION_LABELS = {
    "all": "每条都发（按顺序）",
    "random": "随机挑一条",
    "round_robin": "轮流使用",
}


def _state(request: Request) -> Any:
    return request.app.state.keeper


# =============================================================================
# 读
# =============================================================================


@router.get("")
@router.get("/")
async def get_tasks(request: Request) -> dict[str, Any]:
    """当前完整任务配置。前端的设置页一次拿全。

    一次解析 ``tasks.json``（``load_task_bundle``），不再分别 load_schedule /
    load_messages / load_interval 各自重读一遍（一个请求原本最多解析 5 次）。
    """
    state = _state(request)
    repo = state.repository

    bundle = repo.load_task_bundle()
    schedule = bundle["schedule"]
    messages = bundle["messages"]
    interval = bundle["interval"]
    rotation = bundle["rotation"]

    return {
        "schedule": _schedule_payload(schedule),
        "messages": [_message_payload(m) for m in messages],
        "interval": {"minimum": interval.minimum, "maximum": interval.maximum},
        "rotation": rotation,
        "rotation_label": ROTATION_LABELS.get(rotation, ""),
        "rotation_options": [{"value": k, "label": v} for k, v in ROTATION_LABELS.items()],
        "updated_at": bundle["updated_at"],
        "defaults": {
            "schedule": _schedule_payload(Schedule()),
            "interval": {"minimum": 3.0, "maximum": 8.0},
        },
    }


@router.get("/schedule")
async def get_schedule(request: Request) -> dict[str, Any]:
    """单独取调度配置（含「下次运行」）。"""
    state = _state(request)
    schedule = state.repository.load_schedule()

    payload = _schedule_payload(schedule)
    payload["next_run"] = _next_run_info(state)
    return payload


# =============================================================================
# 调度写
# =============================================================================


class SchedulePayload(BaseModel):
    """调度配置的可编辑字段。"""

    enabled: bool = True
    hour: int = Field(default=10, ge=0, le=23)
    minute: int = Field(default=30, ge=0, le=59)
    jitter_minutes: int = Field(
        default=25,
        ge=0,
        le=720,
        description="在设定时间后的多少分钟内随机发送。0 表示准点发。",
    )
    timezone: str = Field(default="Asia/Shanghai", max_length=64)


@router.put("/schedule")
async def put_schedule(request: Request, payload: SchedulePayload) -> dict[str, Any]:
    """保存调度配置并立刻重排任务。

    时区是这里最容易出错的一项：错了就是 8 小时偏差，而火花按自然日算，
    偏差会直接导致某天漏发。所以时区解析失败时**报错而不是静默兜底**。
    """
    state = _state(request)

    from ...scheduler.jobs import resolve_timezone

    try:
        # 构造 Schedule 就会触发范围校验（hour/minute/jitter）
        schedule = Schedule(
            enabled=payload.enabled,
            hour=payload.hour,
            minute=payload.minute,
            jitter_minutes=payload.jitter_minutes,
            timezone=payload.timezone,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # 时区先验证一次 —— 让错误在保存时就暴露，而不是等到触发时
    try:
        resolve_timezone(schedule.timezone, strict=True)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail=(f"时区 {schedule.timezone!r} 无法解析。请用 IANA 名称，例如 Asia/Shanghai。"),
        ) from exc

    state.repository.save_schedule(schedule)
    LOGGER.info("调度配置已更新：%s", schedule.describe())

    next_run = _apply_reschedule(state, schedule)

    return {
        "ok": True,
        "detail": f"已保存：{schedule.describe()}",
        "schedule": _schedule_payload(schedule),
        "next_run": next_run,
        "updated_at": now_str(),
    }


# =============================================================================
# 消息池
# =============================================================================


class MessagePayload(BaseModel):
    """一条消息。字段与 :class:`Message` 对齐。"""

    kind: str = Field(default="text", pattern="^(text|image|sticker|random)$")
    content: str | None = None
    path: str | None = None
    sticker: str | None = None
    choices: list[MessagePayload] = Field(default_factory=list)


class MessagesPayload(BaseModel):
    """整池替换。"""

    messages: list[MessagePayload] = Field(default_factory=list)


@router.put("/messages")
async def put_messages(request: Request, payload: MessagesPayload) -> dict[str, Any]:
    """整池替换消息列表。

    为什么是整池替换而不是单条增删：消息池的本质是「一小撮文案」，
    前端是一个可上下移动的列表。整池提交能让顺序（对 ``all`` 策略有意义）
    准确落盘，也避免了一堆 id 管理。
    """
    state = _state(request)
    repo = state.repository

    try:
        messages = tuple(_message_from_payload(item) for item in payload.messages)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    tasks = repo.load_tasks()
    tasks["messages"] = [_message_payload(m) for m in messages]
    repo.save_tasks(tasks)

    LOGGER.info("消息池已更新，共 %d 条", len(messages))

    return {
        "ok": True,
        "detail": f"已保存 {len(messages)} 条消息",
        "messages": [_message_payload(m) for m in messages],
        "updated_at": now_str(),
    }


class RotationPayload(BaseModel):
    """轮换策略。"""

    rotation: str = Field(pattern="^(all|random|round_robin)$")


@router.put("/rotation")
async def put_rotation(request: Request, payload: RotationPayload) -> dict[str, Any]:
    """设置消息轮换策略。"""
    state = _state(request)
    repo = state.repository

    tasks = repo.load_tasks()
    tasks["rotation"] = payload.rotation
    repo.save_tasks(tasks)

    return {
        "ok": True,
        "detail": f"已设为：{ROTATION_LABELS.get(payload.rotation, payload.rotation)}",
        "rotation": payload.rotation,
        "rotation_label": ROTATION_LABELS.get(payload.rotation, ""),
    }


class IntervalPayload(BaseModel):
    """收件人之间的随机间隔（秒）。"""

    minimum: float = Field(default=3.0, ge=0.0, le=3600.0)
    maximum: float = Field(default=8.0, ge=0.0, le=3600.0)


@router.put("/interval")
async def put_interval(request: Request, payload: IntervalPayload) -> dict[str, Any]:
    """设置收件人之间的发送间隔。"""
    if payload.maximum < payload.minimum:
        raise HTTPException(status_code=422, detail="最大值不能小于最小值")

    state = _state(request)
    repo = state.repository

    tasks = repo.load_tasks()
    tasks["interval"] = {"minimum": payload.minimum, "maximum": payload.maximum}
    repo.save_tasks(tasks)

    return {
        "ok": True,
        "detail": f"间隔已设为 {payload.minimum:.0f}–{payload.maximum:.0f} 秒",
        "interval": {"minimum": payload.minimum, "maximum": payload.maximum},
    }


# =============================================================================
# 预览与校验
# =============================================================================


@router.get("/preview")
async def preview(request: Request, samples: int = Query(default=5, ge=1, le=20)) -> dict[str, Any]:
    """预览「按当前配置会怎么发」。

    用一个纯本地的方式模拟一次任务计划：挑哪些人、用哪些文案、大概什么时间。
    不发任何请求，不改任何东西 —— 让用户在真正开启定时之前先看一眼。
    """
    state = _state(request)
    repo = state.repository

    bundle = repo.load_task_bundle()
    schedule = bundle["schedule"]
    messages = bundle["messages"]
    interval = bundle["interval"]
    rotation = bundle["rotation"]
    contacts = repo.enabled_contacts()

    # 消息为空时 run_once 会兜底用「早」，预览要对齐这个行为
    effective = messages or (Message(kind="text", content="早"),)

    return {
        "schedule": _schedule_payload(schedule),
        "schedule_text": schedule.describe(),
        "recipients": [c.name for c in contacts],
        "recipient_count": len(contacts),
        "messages": [_message_payload(m) for m in effective],
        "used_fallback_message": not messages,
        "interval": {"minimum": interval.minimum, "maximum": interval.maximum},
        "rotation": rotation,
        "rotation_label": ROTATION_LABELS.get(rotation, ""),
        "warnings": _preview_warnings(schedule, contacts, messages),
    }


def _preview_warnings(schedule: Schedule, contacts: tuple[Any, ...], messages: tuple[Any, ...]) -> list[str]:
    """把「配置能保存但可能不是你想要」的地方提前说出来。"""
    warnings: list[str] = []

    if not schedule.enabled:
        warnings.append("定时发送当前是关闭的 —— 不会自动发。")
    if not contacts:
        warnings.append("还没有启用任何收件人 —— 任务会被跳过。")
    if not messages:
        warnings.append("消息池是空的，届时会用默认文案「早」。建议至少配一条常用的。")
    if schedule.jitter_minutes == 0:
        warnings.append("抖动为 0，每天会准点发送 —— 时间过于整齐，建议留一点抖动。")
    if schedule.jitter_minutes > 240:
        warnings.append(
            f"抖动有 {schedule.jitter_minutes} 分钟（超过 4 小时），发送窗口很宽，可能晚到你睡觉之后才发。"
        )
    return warnings


# =============================================================================
# 内部
# =============================================================================


def _schedule_payload(schedule: Schedule) -> dict[str, Any]:
    return {
        "enabled": schedule.enabled,
        "hour": schedule.hour,
        "minute": schedule.minute,
        "jitter_minutes": schedule.jitter_minutes,
        "timezone": schedule.timezone,
        "describe": schedule.describe(),
    }


def _message_payload(message: Message) -> dict[str, Any]:
    payload: dict[str, Any] = {"kind": message.kind}
    if message.content is not None:
        payload["content"] = message.content
    if message.path is not None:
        payload["path"] = str(message.path)
    if message.sticker is not None:
        payload["sticker"] = message.sticker
    if message.choices:
        payload["choices"] = [_message_payload(c) for c in message.choices]
    return payload


def _message_from_payload(payload: MessagePayload | dict[str, Any]) -> Message:
    """把请求体转成 :class:`Message`。

    非法值由 ``Message.__post_init__`` 拦下 —— 这里只负责转换，
    不重复实现校验规则。
    """
    if isinstance(payload, dict):
        payload = MessagePayload(**payload)

    kind = payload.kind

    if kind == "random":
        choices = tuple(_message_from_payload(c) for c in payload.choices)
        return Message(kind="random", choices=choices)

    if kind == "image":
        from pathlib import Path

        if not payload.path:
            raise ValueError("图片消息必须提供 path")
        return Message(kind="image", path=Path(payload.path))

    if kind == "sticker":
        return Message(kind="sticker", sticker=payload.sticker or "")

    return Message(kind="text", content=(payload.content or "").strip())


def _next_run_info(state: Any) -> dict[str, Any]:
    """调度器的「下次运行」信息。调度器没跑时返回说明而不是报错。"""
    scheduler = state.scheduler

    if scheduler is None:
        from ...scheduler.jobs import get_singleton

        scheduler = get_singleton()

    if scheduler is None:
        return {
            "running": False,
            "detail": "调度器未启动（可能是 HUOHUA_ENABLE_SCHEDULER=false，或由外部 cron 触发）",
        }

    status = scheduler.status()
    return {
        "running": status.running,
        "enabled": status.enabled,
        "description": status.description,
        "next_run_at": status.next_run_at,
        "next_run_in_seconds": status.next_run_in_seconds,
        "human": scheduler.describe_next(),
    }


def _apply_reschedule(state: Any, schedule: Schedule) -> dict[str, Any]:
    """把新配置推给调度器，让它立刻生效。"""
    scheduler = state.scheduler

    if scheduler is None:
        from ...scheduler.jobs import get_singleton

        scheduler = get_singleton()

    if scheduler is None:
        return {
            "running": False,
            "detail": "配置已保存，但调度器没在跑 —— 重启服务后生效",
        }

    try:
        # 同步 scheduler 里的 settings，否则它仍按旧配置描述状态
        scheduler.settings = state.settings
        scheduler.reschedule(schedule)
    except Exception as exc:
        LOGGER.exception("重排任务失败")
        return {
            "running": True,
            "ok": False,
            "detail": f"配置已保存，但重排任务失败：{type(exc).__name__}: {exc}",
        }

    return _next_run_info(state) | {"rescheduled": True}
