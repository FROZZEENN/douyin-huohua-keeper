"""通知通道实现。

六个通道，每个都是一个小的适配器：把 ``(level, title, body)`` 翻译成对方
要求的请求格式。共同的约定：

- ``validate_config()`` 返回缺失的配置项名字（空列表表示没问题）
- ``send(...)`` 返回 :class:`DeliveryResult`，**不抛异常**
  —— 通知失败是常态（网络、密钥过期、对方限流），
  它必须被记录和上报，而不是炸掉主流程

新增通道只要实现这两个方法，然后在 ``CHANNELS`` 里登记。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from ..models import AlertLevel

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """一次投递的结果。"""

    channel: str
    ok: bool
    detail: str = ""
    attempt: int = 1

    @property
    def failed(self) -> bool:
        return not self.ok


class ChannelError(Exception):
    """通道配置或投递过程中的错误。"""


class Channel:
    """通知通道基类。"""

    name: str = "base"

    # 哪些级别需要推送。子类可以覆盖
    default_min_level: AlertLevel = AlertLevel.WARNING

    def validate_config(self) -> list[str]:
        """返回缺失的配置字段名。空列表表示配置完整。"""
        return []

    def send(self, level: AlertLevel, title: str, body: str, *, timeout: float = 10.0) -> DeliveryResult:
        raise NotImplementedError

    # --- 供子类使用的小工具 -------------------------------------------------

    def _post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        timeout: float,
        headers: dict[str, str] | None = None,
    ) -> DeliveryResult:
        """发一个 JSON POST。

        所有的 HTTP 细节都在这里，子类只需要组装 payload。
        """
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json; charset=utf-8",
                **(headers or {}),
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                if 200 <= response.status < 300:
                    return DeliveryResult(channel=self.name, ok=True, detail=f"HTTP {response.status}")
                return DeliveryResult(
                    channel=self.name,
                    ok=False,
                    detail=f"HTTP {response.status}：{raw[:200]}",
                )
        except urllib.error.HTTPError as exc:
            detail = ""
            # 读响应体失败不影响「通知没发出去」这个结论本身
            with contextlib.suppress(Exception):
                detail = exc.read().decode("utf-8", errors="replace")[:200]
            return DeliveryResult(
                channel=self.name,
                ok=False,
                detail=f"HTTP {exc.code}：{detail or exc.reason}",
            )
        except urllib.error.URLError as exc:
            return DeliveryResult(channel=self.name, ok=False, detail=f"网络错误：{exc.reason}")
        except TimeoutError:
            return DeliveryResult(channel=self.name, ok=False, detail=f"请求超时（{timeout:.0f}s）")
        except Exception as exc:  # noqa: BLE001
            return DeliveryResult(channel=self.name, ok=False, detail=f"{type(exc).__name__}: {exc}")

    def _get(self, url: str, *, timeout: float) -> DeliveryResult:
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                if 200 <= response.status < 300:
                    return DeliveryResult(channel=self.name, ok=True, detail=f"HTTP {response.status}")
                return DeliveryResult(
                    channel=self.name, ok=False, detail=f"HTTP {response.status}：{raw[:200]}"
                )
        except urllib.error.HTTPError as exc:
            return DeliveryResult(channel=self.name, ok=False, detail=f"HTTP {exc.code}")
        except urllib.error.URLError as exc:
            return DeliveryResult(channel=self.name, ok=False, detail=f"网络错误：{exc.reason}")
        except TimeoutError:
            return DeliveryResult(channel=self.name, ok=False, detail="请求超时")
        except Exception as exc:  # noqa: BLE001
            return DeliveryResult(channel=self.name, ok=False, detail=f"{type(exc).__name__}: {exc}")


# =============================================================================
# 各通道实现
# =============================================================================


class BarkChannel(Channel):
    """Bark（iOS 推送）。

    配置最简单的一个：URL 里自带密钥，GET 请求就能推。
    形如 ``https://api.day.app/你的Key``
    """

    name = "bark"

    def __init__(self, url: str) -> None:
        self.url = (url or "").strip().rstrip("/")

    def validate_config(self) -> list[str]:
        return [] if self.url else ["HUOHUA_BARK_URL"]

    def send(self, level: AlertLevel, title: str, body: str, *, timeout: float = 10.0) -> DeliveryResult:
        # Bark 的路径形式：/{key}/{title}/{body}
        # 用 urlencode 保证标题和正文里的特殊字符不会破坏 URL
        safe_title = urllib.parse.quote(title, safe="")
        safe_body = urllib.parse.quote(body, safe="")

        params = {
            "level": "critical" if level.urgent else "active",
            "group": "douyin-huohua-keeper",
        }
        # 紧急级别用「持续响铃」，普通级别静默推送
        if level.urgent:
            params["sound"] = "alarm"
            params["call"] = "1"

        query = urllib.parse.urlencode(params)
        url = f"{self.url}/{safe_title}/{safe_body}?{query}"

        return self._get(url, timeout=timeout)


class ServerChanChannel(Channel):
    """Server酱（微信推送）。

    形如 ``https://sctapi.ftqq.com/<SendKey>.send``
    """

    name = "serverchan"

    def __init__(self, key: str) -> None:
        self.key = (key or "").strip()

    def validate_config(self) -> list[str]:
        return [] if self.key else ["HUOHUA_SERVERCHAN_KEY"]

    def send(self, level: AlertLevel, title: str, body: str, *, timeout: float = 10.0) -> DeliveryResult:
        url = f"https://sctapi.ftqq.com/{self.key}.send"
        return self._post_json(
            url,
            {"title": title, "desp": body},
            timeout=timeout,
        )


class DingTalkChannel(Channel):
    """钉钉机器人。

    需要 webhook 和 secret 两个值 —— 群里常见「只填了 webhook」然后
    报签名错误的情况，所以 ``validate_config`` 两个都要求。
    """

    name = "dingtalk"

    def __init__(self, webhook: str, secret: str) -> None:
        self.webhook = (webhook or "").strip()
        self.secret = (secret or "").strip()

    def validate_config(self) -> list[str]:
        missing = []
        if not self.webhook:
            missing.append("HUOHUA_DINGTALK_WEBHOOK")
        if not self.secret:
            missing.append("HUOHUA_DINGTALK_SECRET")
        return missing

    def _sign(self) -> str:
        """计算加签。

        钉钉要求把 ``timestamp`` 和 ``HmacSHA256(secret, timestamp\\nsecret)``
        一起拼到 URL 上，且 timestamp 与服务器时间偏差不能超过 1 小时。
        """
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{self.secret}"
        digest = hmac.new(
            self.secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(digest).decode("utf-8"))
        return f"timestamp={timestamp}&sign={sign}"

    def send(self, level: AlertLevel, title: str, body: str, *, timeout: float = 10.0) -> DeliveryResult:
        separator = "&" if "?" in self.webhook else "?"
        url = f"{self.webhook}{separator}{self._sign()}"

        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": f"### {title}\n\n{body}",
            },
        }
        return self._post_json(url, payload, timeout=timeout)


class FeishuChannel(Channel):
    """飞书机器人。"""

    name = "feishu"

    def __init__(self, webhook: str) -> None:
        self.webhook = (webhook or "").strip()

    def validate_config(self) -> list[str]:
        return [] if self.webhook else ["HUOHUA_FEISHU_WEBHOOK"]

    def send(self, level: AlertLevel, title: str, body: str, *, timeout: float = 10.0) -> DeliveryResult:
        payload = {
            "msg_type": "post",
            "content": {
                "post": {
                    "zh_cn": {
                        "title": title,
                        "content": [[{"tag": "text", "text": body}]],
                    }
                }
            },
        }
        return self._post_json(self.webhook, payload, timeout=timeout)


class TelegramChannel(Channel):
    """Telegram Bot。

    注意：Telegram 的 API 在部分网络环境下访问不了。如果投递持续失败，
    先确认服务器能连上 ``api.telegram.org``。
    """

    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token = (bot_token or "").strip()
        self.chat_id = (chat_id or "").strip()

    def validate_config(self) -> list[str]:
        missing = []
        if not self.bot_token:
            missing.append("HUOHUA_TELEGRAM_BOT_TOKEN")
        if not self.chat_id:
            missing.append("HUOHUA_TELEGRAM_CHAT_ID")
        return missing

    def send(self, level: AlertLevel, title: str, body: str, *, timeout: float = 10.0) -> DeliveryResult:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

        text = f"*{_escape_markdown(title)}*\n\n{_escape_markdown(body)}"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "MarkdownV2",
            "disable_notification": not level.urgent,
        }
        return self._post_json(url, payload, timeout=timeout)


class GenericWebhookChannel(Channel):
    """通用 Webhook。

    发一个 JSON，字段是固定的：
    ``{"level", "title", "body", "source", "timestamp"}``

    方便对接自建服务、n8n、Node-RED 之类。
    """

    name = "webhook"

    def __init__(self, url: str) -> None:
        self.url = (url or "").strip()

    def validate_config(self) -> list[str]:
        return [] if self.url else ["HUOHUA_GENERIC_WEBHOOK"]

    def send(self, level: AlertLevel, title: str, body: str, *, timeout: float = 10.0) -> DeliveryResult:
        payload = {
            "level": level.value,
            "urgent": level.urgent,
            "title": title,
            "body": body,
            "source": "douyin-huohua-keeper",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        return self._post_json(self.url, payload, timeout=timeout)


# =============================================================================
# 工厂
# =============================================================================


def build_channel(name: str, settings: Any) -> Channel | None:
    """按名字构造通道。未知名字返回 None（并记日志）。"""
    name = (name or "").strip().lower()

    if name == "bark":
        return BarkChannel(settings.bark_url)
    if name == "serverchan":
        return ServerChanChannel(settings.serverchan_key)
    if name == "dingtalk":
        return DingTalkChannel(settings.dingtalk_webhook, settings.dingtalk_secret)
    if name == "feishu":
        return FeishuChannel(settings.feishu_webhook)
    if name == "telegram":
        return TelegramChannel(settings.telegram_bot_token, settings.telegram_chat_id)
    if name == "webhook":
        return GenericWebhookChannel(settings.generic_webhook)

    LOGGER.warning("未知的通知通道：%r", name)
    return None


def build_channels(settings: Any) -> tuple[Channel, ...]:
    """按配置构造所有已启用的通道。"""
    channels: list[Channel] = []
    for name in getattr(settings, "channels", ()) or ():
        channel = build_channel(name, settings)
        if channel is None:
            continue
        missing = channel.validate_config()
        if missing:
            LOGGER.error(
                "通知通道 %s 配置不完整，已跳过。缺少：%s",
                channel.name,
                ", ".join(missing),
            )
            continue
        channels.append(channel)
    return tuple(channels)


def _escape_markdown(text: str) -> str:
    """转义 Telegram MarkdownV2 的特殊字符。

    MarkdownV2 要求转义一批字符，漏一个就会让整条消息发送失败并返回
    400 —— 而报错信息不会告诉你哪个字符的问题。干脆全转义。
    """
    special = r"_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{ch}" if ch in special else ch for ch in text)
