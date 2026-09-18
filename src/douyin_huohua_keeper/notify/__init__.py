"""分级告警。

核心原则：**通知自己也会失败，而且绝不允许静默失败。**

- ``channels``  —— Bark / Server酱 / 钉钉 / 飞书 / Telegram / 通用 Webhook
                   每个通道实现 ``validate_config()`` 和 ``send()``，
                   ``send()`` 返回结果而不是抛异常
- ``dispatcher``—— 按级别筛选通道、逐个投递、失败重试、汇总投递结果，
                   并把「全部失败」这种情况明确记成 error 日志

一个通道挂了不应该让主流程崩掉，但必须在运行报告里留下痕迹 —— 看得见的
降级好过看不见的沉默。
"""

from __future__ import annotations

from ..models import AlertLevel
from .channels import (
    BarkChannel,
    Channel,
    ChannelError,
    DeliveryResult,
    DingTalkChannel,
    FeishuChannel,
    GenericWebhookChannel,
    ServerChanChannel,
    TelegramChannel,
    build_channel,
    build_channels,
)
from .dispatcher import Alert, Dispatcher, DispatchReport, level_from_state

__all__ = [
    "Alert",
    # AlertLevel 定义在 models 里，但通知是本项目里最常和它打交道的地方 ——
    # 从 ``douyin_huohua_keeper.notify`` 一并导出，省得调用方到处找它是谁家的。
    # （回归：曾经漏了这个导出，导致 ``from ...notify import AlertLevel`` 直接
    #  ImportError → 工作台的「发送测试通知」接口 500。）
    "AlertLevel",
    "BarkChannel",
    "Channel",
    "ChannelError",
    "DeliveryResult",
    "DingTalkChannel",
    "DispatchReport",
    "Dispatcher",
    "FeishuChannel",
    "GenericWebhookChannel",
    "ServerChanChannel",
    "TelegramChannel",
    "build_channel",
    "build_channels",
    "level_from_state",
]
