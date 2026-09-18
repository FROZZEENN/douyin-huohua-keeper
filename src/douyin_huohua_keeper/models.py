"""数据模型。

设计原则：
- 全部使用 frozen dataclass，避免运行期被意外修改；
- 不依赖任何第三方库，便于单元测试；
- 序列化逻辑与模型放在一起，避免散落各处。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Literal

# ============================================================
# 消息
# ============================================================

MessageKind = Literal["text", "image", "sticker", "random"]


class StickerTarget(str, Enum):
    """原生表情的定位方式，按优先级尝试。"""

    DESCRIPTION = "description"  # 按表情描述文本精确匹配
    ACCESSIBLE = "accessible"  # 按无障碍名称匹配
    ATTRIBUTE = "attribute"  # 按 aria-label / title / alt 匹配
    INDEX = "index"  # 按序号兜底


@dataclass(frozen=True, slots=True)
class Message:
    """一条待发送的消息。

    ``kind`` 决定使用哪个字段：
    - text    -> content
    - image   -> path
    - sticker -> sticker
    - random  -> choices（发送时随机挑一个）
    """

    kind: MessageKind
    content: str | None = None
    path: Path | None = None
    sticker: str | None = None
    choices: tuple[Message, ...] = ()

    def __post_init__(self) -> None:
        if self.kind == "text" and not (self.content or "").strip():
            raise ValueError("text 消息必须提供非空 content")
        if self.kind == "image" and self.path is None:
            raise ValueError("image 消息必须提供 path")
        if self.kind == "sticker" and not (self.sticker or "").strip():
            raise ValueError("sticker 消息必须提供 sticker")
        if self.kind == "random":
            if not self.choices:
                raise ValueError("random 消息必须提供非空 choices")
            if any(choice.kind == "random" for choice in self.choices):
                raise ValueError("random 消息不支持嵌套")


@dataclass(frozen=True, slots=True)
class StickerSpec:
    """原生表情的定位配置。"""

    name: str
    category: str | None = None
    description: str | None = None
    accessible_name: str | None = None
    attribute_name: str | None = None
    index: int | None = None

    def locate_order(self) -> tuple[StickerTarget, ...]:
        """返回可用的定位方式，按优先级排序。"""
        order: list[StickerTarget] = []
        if self.description:
            order.append(StickerTarget.DESCRIPTION)
        if self.accessible_name:
            order.append(StickerTarget.ACCESSIBLE)
        if self.attribute_name:
            order.append(StickerTarget.ATTRIBUTE)
        if self.index is not None:
            order.append(StickerTarget.INDEX)
        if not order:
            raise ValueError(f"表情 {self.name} 未配置任何可用的定位方式")
        return tuple(order)


# ============================================================
# 联系人
# ============================================================


@dataclass(frozen=True, slots=True)
class Contact:
    """一个可发送的联系人。"""

    name: str
    conversation_id: str | None = None
    is_group: bool = False
    note: str | None = None
    enabled: bool = True
    weight: int = 1
    last_sent_on: str | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("联系人名称不能为空")
        if self.weight < 0:
            raise ValueError("权重不能为负")

    def sent_today(self, today: str) -> bool:
        """今天是否已经发过。用于防重复。"""
        return self.last_sent_on == today


@dataclass(frozen=True, slots=True)
class ContactBook:
    """联系人缓存，来自工作台刷新。"""

    account_id: str
    refreshed_at: str
    contacts: tuple[Contact, ...]

    def names(self) -> tuple[str, ...]:
        return tuple(contact.name for contact in self.contacts)

    def find(self, name: str) -> Contact | None:
        for contact in self.contacts:
            if contact.name == name:
                return contact
        return None


# ============================================================
# 账号与配置
# ============================================================


@dataclass(frozen=True, slots=True)
class Account:
    """一个抖音账号。"""

    id: str
    label: str
    enabled: bool = True
    state_file: str | None = None
    cookie_file: str | None = None

    def credentials_configured(self) -> bool:
        return bool(self.state_file or self.cookie_file)


@dataclass(frozen=True, slots=True)
class SendInterval:
    """消息之间的随机间隔（秒）。"""

    minimum: float = 3.0
    maximum: float = 8.0

    def __post_init__(self) -> None:
        if self.minimum < 0:
            raise ValueError("间隔最小值不能为负")
        if self.maximum < self.minimum:
            raise ValueError("间隔最大值不能小于最小值")


@dataclass(frozen=True, slots=True)
class Schedule:
    """定时配置。"""

    enabled: bool = True
    hour: int = 10
    minute: int = 30
    jitter_minutes: int = 25
    timezone: str = "Asia/Shanghai"

    def __post_init__(self) -> None:
        if not 0 <= self.hour <= 23:
            raise ValueError("hour 必须在 0-23 之间")
        if not 0 <= self.minute <= 59:
            raise ValueError("minute 必须在 0-59 之间")
        if self.jitter_minutes < 0:
            raise ValueError("抖动分钟数不能为负")
        if self.jitter_minutes > 720:
            raise ValueError("抖动分钟数不应超过 720（12 小时），否则发送窗口会跨过午夜")

    def describe(self) -> str:
        """人类可读的描述，工作台直接显示这句话。"""
        if not self.enabled:
            return "已关闭"
        if self.jitter_minutes == 0:
            return f"每天 {self.hour:02d}:{self.minute:02d}"

        total = self.hour * 60 + self.minute + self.jitter_minutes
        crossed = total >= 1440
        end_hour = (total // 60) % 24
        end_minute = total % 60

        text = f"每天 {self.hour:02d}:{self.minute:02d} 起 {self.jitter_minutes} 分钟内随机（约至 {end_hour:02d}:{end_minute:02d}）"
        if crossed:
            text += "，已跨过午夜"
        return text


@dataclass(frozen=True, slots=True)
class TaskPlan:
    """一次发送任务的完整计划。

    这是「冻结」后的产物：任务开始时从配置里读出来，中途不再变化，
    这样即使你在运行过程中改了网页配置，也不影响正在跑的这一轮。
    """

    task_id: str
    targets: tuple[Contact, ...]
    messages: tuple[Message, ...]
    interval: SendInterval = field(default_factory=SendInterval)
    prevent_duplicates: bool = True
    continue_on_error: bool = True
    open_retries: int = 2
    page_timeout_seconds: float = 15.0
    human_typing: bool = False
    streak_start_date: str | None = None

    def __post_init__(self) -> None:
        if not self.targets:
            raise ValueError("任务必须至少指定一个收件人")
        if not self.messages:
            raise ValueError("任务必须至少指定一条消息")

    @property
    def recipient_names(self) -> tuple[str, ...]:
        return tuple(contact.name for contact in self.targets)


# ============================================================
# 运行结果
# ============================================================


class RunStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNCERTAIN = "uncertain"


class FailureKind(str, Enum):
    """失败分类，决定重试与告警策略。"""

    TRANSIENT = "transient"  # 临时性：网络抖动、渲染慢
    PERMANENT = "permanent"  # 永久性：好友不存在
    AUTH = "auth"  # 认证失效：必须重新登录
    RISK = "risk"  # 风控：必须立即停止
    CONFIG = "config"  # 配置错误
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TargetOutcome:
    """单个联系人的发送结果。"""

    name: str
    status: RunStatus
    sent: int = 0
    failure_kind: FailureKind | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class RunReport:
    """一次运行的完整报告。"""

    run_id: str
    account_id: str
    started_at: str
    finished_at: str
    dry_run: bool
    outcomes: tuple[TargetOutcome, ...]
    consecutive_failures: int = 0

    @property
    def succeeded(self) -> tuple[TargetOutcome, ...]:
        return tuple(item for item in self.outcomes if item.status == RunStatus.SUCCESS)

    @property
    def failed(self) -> tuple[TargetOutcome, ...]:
        return tuple(item for item in self.outcomes if item.status == RunStatus.FAILED)

    @property
    def all_succeeded(self) -> bool:
        return bool(self.outcomes) and not self.failed

    def summary(self) -> str:
        total = len(self.outcomes)
        return f"成功 {len(self.succeeded)}/{total}，失败 {len(self.failed)}"


# ============================================================
# 通知
# ============================================================


class AlertLevel(str, Enum):
    """告警级别，决定通知通道与文案强度。

    级别从低到高：NORMAL < NOTICE < WARNING < CRITICAL < RISK
    """

    NORMAL = "normal"  # 全部成功
    NOTICE = "notice"  # 单次失败（但可能已重试成功）
    WARNING = "warning"  # 连续失败达警告阈值
    CRITICAL = "critical"  # 连续失败达严重阈值
    RISK = "risk"  # 登录态失效 / 命中风控

    @property
    def urgent(self) -> bool:
        """是否需要强提醒（响铃、置顶、绕过免打扰）。

        只有 CRITICAL 和 RISK 值得把你从睡梦里叫醒 ——
        其余级别都是「你方便的时候看一眼」。
        """
        return self in (AlertLevel.CRITICAL, AlertLevel.RISK)

    @property
    def silent_by_default(self) -> bool:
        """默认是否不推送。

        **只有 NORMAL 静默**（天天推「一切正常」只会让人屏蔽你）。

        NOTICE **必须推** —— 它代表「这一轮真的有人没发出去」。
        这条改过一次，背景是一次真实的静默失败：

            NOTICE 以前也静默，而「连续失败」只在**跨天**累计，
            于是「今天有 3 个人没收到消息」这件事，要等到第二天又失败
            才会被告知 —— 用户连着几天没发现火花断了，直到手动去看才发现。
            用户的原话是「失败两天了才提示」。

        现在的原则：**任何一轮里出现了真实失败，就地推。
        连续失败天数只用来决定告警的严重级别，不再决定「推不推」。**
        """
        return self is AlertLevel.NORMAL


# ============================================================
# 序列化辅助
# ============================================================


def message_to_dict(message: Message) -> dict[str, Any]:
    """把 Message 转成可 JSON 序列化的字典。"""
    payload: dict[str, Any] = {"kind": message.kind}
    if message.content is not None:
        payload["content"] = message.content
    if message.path is not None:
        payload["path"] = str(message.path)
    if message.sticker is not None:
        payload["sticker"] = message.sticker
    if message.choices:
        payload["choices"] = [message_to_dict(choice) for choice in message.choices]
    return payload


def contact_to_dict(contact: Contact) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": contact.name}
    if contact.conversation_id:
        payload["conversation_id"] = contact.conversation_id
    if contact.is_group:
        payload["is_group"] = True
    if contact.note:
        payload["note"] = contact.note
    if not contact.enabled:
        payload["enabled"] = False
    if contact.weight != 1:
        payload["weight"] = contact.weight
    if contact.last_sent_on:
        payload["last_sent_on"] = contact.last_sent_on
    return payload


def outcome_to_dict(outcome: TargetOutcome) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": outcome.name, "status": outcome.status.value, "sent": outcome.sent}
    if outcome.failure_kind is not None:
        payload["failure_kind"] = outcome.failure_kind.value
    if outcome.detail:
        payload["detail"] = outcome.detail
    return payload


def report_to_dict(report: RunReport) -> dict[str, Any]:
    return {
        "run_id": report.run_id,
        "account_id": report.account_id,
        "started_at": report.started_at,
        "finished_at": report.finished_at,
        "dry_run": report.dry_run,
        "consecutive_failures": report.consecutive_failures,
        "summary": report.summary(),
        "outcomes": [outcome_to_dict(item) for item in report.outcomes],
    }
