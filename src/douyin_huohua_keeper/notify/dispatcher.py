"""告警分发。

三件事：

1. **按级别筛选通道**。不是所有级别都要打扰你 —— 一次重试成功的失败
   没必要推送到手机上，那会让人对告警逐渐麻木。
2. **投递带重试**。通知本身也会失败（网络抖动、对方限流）。
3. **投递结果必须留存**。发不出去这件事本身要被记录下来，
   而不是「通知失败了但没人知道」—— 那是最糟糕的失败模式。

分级策略：

===========  ==================  ==========================================
级别          默认是否推送         典型场景
===========  ==================  ==========================================
NORMAL       否                   一切正常（只在网页上看）
NOTICE       否                   单次失败但重试成功
WARNING      是                   连续失败达阈值
CRITICAL     是（强提醒）          连续失败超过阈值
RISK         是（立即）            登录态失效 / 疑似风控
===========  ==================  ==========================================
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..models import AlertLevel
from .channels import Channel, DeliveryResult, build_channels

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Alert:
    """一条告警。"""

    level: AlertLevel
    title: str
    body: str
    # 附加上下文，会出现在通用 webhook 里
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DispatchReport:
    """一次分发的完整结果。"""

    level: AlertLevel
    attempted: tuple[DeliveryResult, ...]
    skipped_reason: str = ""

    @property
    def any_succeeded(self) -> bool:
        return any(result.ok for result in self.attempted)

    @property
    def failed_channels(self) -> tuple[str, ...]:
        return tuple(result.channel for result in self.attempted if result.failed)

    @property
    def succeeded_channels(self) -> tuple[str, ...]:
        return tuple(result.channel for result in self.attempted if result.ok)

    def summary(self) -> str:
        if self.skipped_reason:
            return f"未推送（{self.skipped_reason}）"
        if not self.attempted:
            return "没有可用的通知通道"
        if self.any_succeeded:
            return f"已推送至 {', '.join(self.succeeded_channels)}"
        return f"全部通道失败：{', '.join(self.failed_channels)}"


class Dispatcher:
    """告警分发器。"""

    def __init__(
        self,
        channels: tuple[Channel, ...],
        *,
        retries: int = 2,
        timeout: float = 10.0,
    ) -> None:
        self.channels = channels
        self.retries = max(0, retries)
        self.timeout = timeout

    @classmethod
    def from_settings(cls, settings: Any) -> Dispatcher:
        return cls(
            build_channels(settings),
            retries=getattr(settings, "retries", 2),
            timeout=float(getattr(settings, "timeout_sec", 10)),
        )

    # --- 对外 ---------------------------------------------------------------

    def dispatch(self, alert: Alert, *, minimum_level: AlertLevel | None = None) -> DispatchReport:
        """分发一条告警。

        ``minimum_level`` 决定「非要紧的告警要不要推」。
        传 None 时用级别的默认行为（NORMAL 静默）。
        """
        if not self.channels:
            return DispatchReport(
                level=alert.level,
                attempted=(),
                skipped_reason="未配置任何通知通道",
            )

        if minimum_level is not None and not _meets_level(alert.level, minimum_level):
            return DispatchReport(
                level=alert.level,
                attempted=(),
                skipped_reason=f"级别 {alert.level.value} 低于推送门槛 {minimum_level.value}",
            )

        if minimum_level is None and alert.level.silent_by_default:
            return DispatchReport(
                level=alert.level,
                attempted=(),
                skipped_reason=f"级别 {alert.level.value} 默认不推送",
            )

        results: list[DeliveryResult] = []
        for channel in self.channels:
            results.append(self._deliver_with_retry(channel, alert))

        report = DispatchReport(level=alert.level, attempted=tuple(results))

        # 投递失败本身要留下痕迹 —— 这是「静默失败」最容易藏身的地方
        if results and not report.any_succeeded:
            LOGGER.error(
                "告警推送全部失败（级别 %s）：%s",
                alert.level.value,
                "; ".join(f"{r.channel}: {r.detail}" for r in results),
            )
        elif report.failed_channels:
            LOGGER.warning(
                "部分通道推送失败：%s",
                "; ".join(f"{r.channel}: {r.detail}" for r in results if r.failed),
            )

        return report

    def send_test(self) -> DispatchReport:
        """发一条测试消息。工作台的「测试通知」按钮用它。"""
        alert = Alert(
            level=AlertLevel.WARNING,
            title="🔔 测试通知",
            body=(
                "这是一条来自 douyin-huohua-keeper 的测试通知。\n\n"
                "看到这条消息说明通知通道配置正确，"
                "以后出现连续失败或登录态失效时你会收到提醒。"
            ),
        )
        return self.dispatch(alert)

    # --- 内部 ---------------------------------------------------------------

    def _deliver_with_retry(self, channel: Channel, alert: Alert) -> DeliveryResult:
        """投递一个通道，失败时重试。

        重试间隔很短（1s、2s）—— 通知的时效性比吞吐重要，
        而且如果真的连不上，多等几秒也没用。
        """
        last: DeliveryResult | None = None

        for attempt in range(1, self.retries + 2):
            result = channel.send(alert.level, alert.title, alert.body, timeout=self.timeout)

            if result.ok:
                return DeliveryResult(
                    channel=result.channel,
                    ok=True,
                    detail=result.detail,
                    attempt=attempt,
                )

            last = result
            LOGGER.debug(
                "通道 %s 第 %d 次投递失败：%s",
                channel.name,
                attempt,
                result.detail,
            )

            if attempt <= self.retries:
                time.sleep(min(1.0 * attempt, 3.0))

        assert last is not None
        return DeliveryResult(
            channel=last.channel,
            ok=False,
            detail=last.detail,
            attempt=self.retries + 1,
        )


def level_from_state(
    *,
    consecutive_failures: int,
    auth_expired: bool = False,
    risk_detected: bool = False,
    failed_now: int = 0,
    uncertain_now: int = 0,
    warn_threshold: int = 2,
    critical_threshold: int = 3,
) -> AlertLevel:
    """根据运行状态推断应该发什么级别的告警。

    这是「分级」的判定核心，单独抽出来便于测试和工作台复用。

    **优先级：风控 > 认证失效 > 连续失败天数 > 本轮是否有人失败。**

    前两个需要立即行动；连续失败天数是趋势性的；``failed_now`` 描述的是
    **眼前这一轮**有没有人真的没收到 —— 它必须能独立触发告警。

    为什么要有 ``failed_now``（2026-09 修的真实问题）：

        ``consecutive_failures`` 是**按天**累计的，而且同一天里
        「先成功过、后全失败」不会让它加一。于是会出现这种最坏情况：

            早上 09:30 那次全失败 → 计入 1 天
            手动重试一次成功了 → 连续失败归零
            晚上又跑一次、15 个人全失败 → consecutive_failures 仍是 0
            ⇒ 级别判成 NORMAL ⇒ **一条通知都不发**

        用户以为一切正常，实际当天最后一轮一个人都没发出去。
        所以「这一轮有没有失败」必须有独立的、不依赖跨天计数的判据。

    阈值语义（不变）：阈值为 0 表示**该级别不启用**；两个都为 0 时只保留 NOTICE。
    """
    if risk_detected or auth_expired:
        # 这两种情况必须立刻叫人，与失败计数无关 ——
        # 即使今天只是第一次失败，登录态失效也意味着明天还会失败
        return AlertLevel.RISK

    if critical_threshold > 0 and consecutive_failures >= critical_threshold:
        return AlertLevel.CRITICAL
    if warn_threshold > 0 and consecutive_failures >= warn_threshold:
        return AlertLevel.WARNING

    # 只要「有过真实失败」就该说一声 —— 无论是跨天累计的，还是就是这一轮的
    if consecutive_failures > 0 or failed_now > 0 or uncertain_now > 0:
        return AlertLevel.NOTICE

    return AlertLevel.NORMAL


def _meets_level(actual: AlertLevel, minimum: AlertLevel) -> bool:
    """判断级别是否达到门槛。"""
    order = [
        AlertLevel.NORMAL,
        AlertLevel.NOTICE,
        AlertLevel.WARNING,
        AlertLevel.CRITICAL,
        AlertLevel.RISK,
    ]
    try:
        return order.index(actual) >= order.index(minimum)
    except ValueError:
        return False
