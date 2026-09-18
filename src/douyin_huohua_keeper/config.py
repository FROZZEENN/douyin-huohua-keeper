"""配置加载。

所有配置只有一个来源：**环境变量**（以及可选的 ``.env``）。

为什么不做「配置文件 + 环境变量」双轨：两套来源就意味着两套优先级规则，
出了问题时第一件事先要搞清楚「到底读的是哪个值」。环境变量足够表达这个项目
需要的所有配置，而且和 Docker / systemd 的集成方式天然一致。

``HUOHUA_`` 前缀避免和系统里其他变量撞名。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from .models import Schedule, SendInterval

PREFIX = "HUOHUA_"

# --- 默认值集中在这里，方便一眼看全 -----------------------------------------
DEFAULTS = {
    "HOST": "0.0.0.0",
    "PORT": "8787",
    "DATA_DIR": "data",
    "LOG_DIR": "logs",
    "HEADLESS": "true",
    "BROWSER_TIMEOUT_MS": "60000",
    "NAV_TIMEOUT_MS": "45000",
    "ACTION_TIMEOUT_MS": "15000",
    "SLOW_MO_MS": "0",
    "CONFIRM_TIMEOUT_MS": "15000",
    "SEND_GAP_MIN_SEC": "20",
    "SEND_GAP_MAX_SEC": "90",
    "MAX_RETRIES": "3",
    "RETRY_BACKOFF_SEC": "30",
    "RISK_COOLDOWN_SEC": "300",
    "WARN_THRESHOLD": "2",
    "CRITICAL_THRESHOLD": "3",
    "DIGEST_ENABLED": "true",
    "DIGEST_HOUR": "23",
    "DIGEST_MINUTE": "30",
    "NOTIFY_RETRIES": "2",
    "NOTIFY_TIMEOUT_SEC": "10",
    "JITTER_MINUTES": "25",
    "LOG_LEVEL": "INFO",
    "SCHEDULE_HOUR": "10",
    "SCHEDULE_MINUTE": "30",
}


class ConfigError(ValueError):
    """配置有问题，且无法用默认值兜住。启动时就该明确报出来。"""


def _raw(name: str) -> str:
    return (os.environ.get(PREFIX + name) or "").strip()


def _raw_unprefixed(name: str) -> str:
    """读取不带 ``HUOHUA_`` 前缀的环境变量。

    只有 ``TZ`` 用得上 —— 它是操作系统层面的标准变量，
    Docker 和 systemd 都直接用它，加前缀反而会让容器时区设置失效。
    """
    return (os.environ.get(name) or "").strip()


def _str(name: str, default: str = "") -> str:
    value = _raw(name)
    return value if value else default


def _int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = _raw(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{PREFIX}{name} 必须是整数，当前值：{raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{PREFIX}{name} 不能小于 {minimum}，当前值：{value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{PREFIX}{name} 不能大于 {maximum}，当前值：{value}")
    return value


def _float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = _raw(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{PREFIX}{name} 必须是数字，当前值：{raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{PREFIX}{name} 不能小于 {minimum}，当前值：{value}")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = _raw(name).lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on", "y"}:
        return True
    if raw in {"0", "false", "no", "off", "n"}:
        return False
    raise ConfigError(f"{PREFIX}{name} 必须是布尔值（true/false/1/0），当前值：{raw!r}")


def _csv(name: str) -> tuple[str, ...]:
    raw = _raw(name)
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _optional_path(name: str) -> Path | None:
    raw = _raw(name)
    return Path(raw) if raw else None


def _parse_sandbox_mode(raw: str) -> str:
    """解析 ``HUOHUA_CHROMIUM_NO_SANDBOX``：auto / always / never。

    写错就报错，而不是默默当成 auto —— 这个开关关系到安全性，
    猜错方向（该开却关了）比报错更糟。
    """
    value = (raw or "auto").strip().lower()
    if value not in ("auto", "always", "never"):
        raise ConfigError(f"{PREFIX}CHROMIUM_NO_SANDBOX 只能是 auto / always / never，当前值：{raw!r}")
    return value


def _parse_resource_types(raw: str) -> tuple[str, ...]:
    """解析「要拦掉哪些资源类型」（给 :meth:`BrowserSettings.blocked_resource_types` 用）。

    取值是逗号分隔的 Playwright ``resource_type``：``document`` / ``stylesheet`` /
    ``image`` / ``media`` / ``font`` / ``script`` / ``xhr`` / ``fetch`` 等。

    ``none``（或 ``off``）表示**完全不拦** —— 排障时先关掉它能立刻排除"是不是拦截
    把页面拦坏了"这类嫌疑。
    """
    value = (raw or "").strip().lower()
    if value in ("none", "off", "false", "0"):
        return ()
    return tuple(sorted({item.strip() for item in value.split(",") if item.strip()}))


# =============================================================================
# 分组配置
# =============================================================================


@dataclass(frozen=True, slots=True)
class BrowserSettings:
    """浏览器相关。"""

    headless: bool = True
    browser_path: Path | None = None
    browser_timeout_ms: int = 60_000
    nav_timeout_ms: int = 45_000
    action_timeout_ms: int = 15_000
    slow_mo_ms: int = 0
    # Chromium 沙箱：auto（默认）/ always / never，见 ``browser._needs_no_sandbox``。
    #
    # 为什么需要这个开关：容器里必须关沙箱（没有 user namespace 权限），
    # 但**桌面上没有任何理由关** —— 关了等于让网页代码以你的用户权限运行。
    # auto 会自己判断（容器、或 Linux 上以 root 运行 → 关；普通桌面 → 开）。
    # 只有在排查「是不是沙箱导致页面起不来」时才需要手动设 always。
    chromium_no_sandbox: str = "auto"
    # 要拦掉的资源类型（省流量 + 提速），见 ``_parse_resource_types``。
    #
    # ⚠️ **先看实测，别想当然**（2026-09-19 对 /chat 做过一次流量画像）：
    #     script 20.99MB (75.8%) · stylesheet 2.46MB (8.9%) · xhr 2.13MB (7.7%)
    #     fetch 1.91MB (6.9%) · **image 仅 0.17MB (0.6%)** · 无 font / 无 media
    # 也就是说：**大头是 JS 包，不是图片**；而且这个页面根本不加载字体和音视频。
    # 所以默认拦 font+media 在当前页面上**收益≈0**，留着纯粹是「页面改版后的保险」
    # （比如哪天聊天页开始自动播放视频）。别把它当成省流量主力。
    #
    # 真正能省的是**让 JS/CSS 走缓存**（每个 run 都新建 context，等于每次都重下 21MB），
    # 那需要另做（见评审文档的性能条目）。
    #
    # ⚠️ 默认**不拦 image**：二维码在页面上是 base64 的 data URI（不走网络、拦不到），
    # 但万一抖音改版把它换成网络图片，拦 image 就会直接登录失败 ——
    # 为了 0.6% 的流量去冒"登录不了"的风险完全不值。
    blocked_resource_types: tuple[str, ...] = ("font", "media")
    # 工作台的「共享浏览器」空闲多久后自动关闭（秒）。0 = 不回收。
    #
    # 为什么要它：共享浏览器（同步收件人 / 好友列表用的那个）用过之后会一直
    # 留在进程里，而定时任务又会另起一个 —— 小内存机器上会同时存在两个
    # Chromium（实测约 1.3GB），贴着内存上限跑。空闲回收把常驻压回几十 MB。
    engine_idle_timeout_sec: int = 600

    @classmethod
    def from_env(cls) -> BrowserSettings:
        return cls(
            headless=_bool("HEADLESS", True),
            browser_path=_optional_path("BROWSER_PATH"),
            browser_timeout_ms=_int("BROWSER_TIMEOUT_MS", 60_000, minimum=5_000),
            nav_timeout_ms=_int("NAV_TIMEOUT_MS", 45_000, minimum=5_000),
            action_timeout_ms=_int("ACTION_TIMEOUT_MS", 15_000, minimum=1_000),
            slow_mo_ms=_int("SLOW_MO_MS", 0, minimum=0, maximum=5_000),
            chromium_no_sandbox=_parse_sandbox_mode(_str("CHROMIUM_NO_SANDBOX", "auto")),
            blocked_resource_types=_parse_resource_types(_str("BLOCK_RESOURCE_TYPES", "font,media")),
            engine_idle_timeout_sec=_int("ENGINE_IDLE_TIMEOUT_SEC", 600, minimum=0, maximum=86_400),
        )

    def describe(self) -> str:
        mode = "无头" if self.headless else "有头"
        extra = ""
        if self.slow_mo_ms:
            extra = f"，慢速系数 {self.slow_mo_ms}ms"
        return f"{mode}模式，导航超时 {self.nav_timeout_ms / 1000:.0f}s{extra}"


@dataclass(frozen=True, slots=True)
class SendSettings:
    """发送行为相关。"""

    confirm_timeout_ms: int = 15_000
    gap: SendInterval = field(default_factory=lambda: SendInterval(20.0, 90.0))
    max_retries: int = 3
    retry_backoff_sec: int = 30
    risk_cooldown_sec: int = 300
    dry_run: bool = False

    @classmethod
    def from_env(cls) -> SendSettings:
        gap_min = _float("SEND_GAP_MIN_SEC", 20.0, minimum=0.0)
        gap_max = _float("SEND_GAP_MAX_SEC", 90.0, minimum=0.0)
        if gap_max < gap_min:
            raise ConfigError(
                f"HUOHUA_SEND_GAP_MAX_SEC（{gap_max}）不能小于 HUOHUA_SEND_GAP_MIN_SEC（{gap_min}）"
            )
        return cls(
            confirm_timeout_ms=_int("CONFIRM_TIMEOUT_MS", 15_000, minimum=2_000),
            gap=SendInterval(gap_min, gap_max),
            max_retries=_int("MAX_RETRIES", 3, minimum=0, maximum=10),
            retry_backoff_sec=_int("RETRY_BACKOFF_SEC", 30, minimum=0, maximum=3600),
            risk_cooldown_sec=_int("RISK_COOLDOWN_SEC", 300, minimum=0, maximum=7200),
            dry_run=_bool("DRY_RUN", False),
        )

    def describe(self) -> str:
        parts = [
            f"确认超时 {self.confirm_timeout_ms / 1000:.0f}s",
            f"收件人间隔 {self.gap.minimum:.0f}-{self.gap.maximum:.0f}s",
            f"最多重试 {self.max_retries} 次",
        ]
        if self.dry_run:
            parts.append("**演练模式（不会真正发送）**")
        return "，".join(parts)


@dataclass(frozen=True, slots=True)
class NotifySettings:
    """通知相关。只有当渠道真正被启用时才校验对应的凭据。"""

    channels: tuple[str, ...] = ()
    bark_url: str = ""
    serverchan_key: str = ""
    dingtalk_webhook: str = ""
    dingtalk_secret: str = ""
    feishu_webhook: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    generic_webhook: str = ""
    retries: int = 2
    timeout_sec: int = 10
    warn_threshold: int = 2
    critical_threshold: int = 3
    # 每日汇总：晚上固定时间推一条「今天整体怎么样」的消息。
    # 为什么单独要它：单次告警只描述某一轮，而夜里这条能把
    # 「今天一共跑了几次、成功/失败各几人」一次性说清 —— 用户明确要求过。
    digest_enabled: bool = True
    digest_hour: int = 23
    digest_minute: int = 30

    KNOWN_CHANNELS: ClassVar[tuple[str, ...]] = (
        "bark",
        "serverchan",
        "dingtalk",
        "feishu",
        "telegram",
        "webhook",
    )

    # 每个通道需要哪些字段才能工作
    REQUIRED_FIELDS: ClassVar[dict[str, tuple[str, ...]]] = {
        "bark": ("bark_url",),
        "serverchan": ("serverchan_key",),
        "dingtalk": ("dingtalk_webhook", "dingtalk_secret"),
        "feishu": ("feishu_webhook",),
        "telegram": ("telegram_bot_token", "telegram_chat_id"),
        "webhook": ("generic_webhook",),
    }

    @classmethod
    def from_env(cls) -> NotifySettings:
        channels = tuple(c.lower() for c in _csv("NOTIFY_CHANNELS"))
        warn = _int("WARN_THRESHOLD", 2, minimum=0, maximum=30)
        critical = _int("CRITICAL_THRESHOLD", 3, minimum=0, maximum=30)

        if warn and critical and warn >= critical:
            raise ConfigError(
                f"HUOHUA_WARN_THRESHOLD（{warn}）必须小于 HUOHUA_CRITICAL_THRESHOLD（{critical}），"
                "否则永远不会升级到 CRITICAL"
            )

        return cls(
            channels=channels,
            bark_url=_str("BARK_URL"),
            serverchan_key=_str("SERVERCHAN_KEY"),
            dingtalk_webhook=_str("DINGTALK_WEBHOOK"),
            dingtalk_secret=_str("DINGTALK_SECRET"),
            feishu_webhook=_str("FEISHU_WEBHOOK"),
            telegram_bot_token=_str("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_str("TELEGRAM_CHAT_ID"),
            generic_webhook=_str("GENERIC_WEBHOOK"),
            retries=_int("NOTIFY_RETRIES", 2, minimum=0, maximum=5),
            timeout_sec=_int("NOTIFY_TIMEOUT_SEC", 10, minimum=1, maximum=120),
            warn_threshold=warn,
            critical_threshold=critical,
            digest_enabled=_bool("DIGEST_ENABLED", True),
            digest_hour=_int("DIGEST_HOUR", 23, minimum=0, maximum=23),
            digest_minute=_int("DIGEST_MINUTE", 30, minimum=0, maximum=59),
        )

    def validate(self) -> list[str]:
        """返回配置问题列表。空列表表示没问题。

        这里不抛异常：一个通道配错了不该让整个服务起不来 ——
        你还能通过工作台看到问题并修正。但问题必须被明确列出来。
        """
        problems: list[str] = []
        for channel in self.channels:
            if channel not in self.KNOWN_CHANNELS:
                problems.append(f"未知的通知通道 {channel!r}，可用：{', '.join(self.KNOWN_CHANNELS)}")
                continue
            missing = [f for f in self.REQUIRED_FIELDS[channel] if not getattr(self, f)]
            if missing:
                env_names = ", ".join(f"{PREFIX}{f.upper()}" for f in missing)
                problems.append(f"通道 {channel} 缺少配置：{env_names}")
        return problems

    def enabled(self) -> bool:
        return bool(self.channels)

    def describe(self) -> str:
        if not self.channels:
            return "未启用（失败时不会通知你）"
        return "，".join(self.channels)


@dataclass(frozen=True, slots=True)
class SchedulerSettings:
    """调度相关。"""

    enabled: bool = True
    run_once: bool = False
    schedule: Schedule = field(default_factory=Schedule)

    @classmethod
    def from_env(cls) -> SchedulerSettings:
        hour = _int("SCHEDULE_HOUR", 10, minimum=0, maximum=23)
        minute = _int("SCHEDULE_MINUTE", 30, minimum=0, maximum=59)
        jitter = _int("JITTER_MINUTES", 25, minimum=0, maximum=720)

        return cls(
            enabled=_bool("ENABLE_SCHEDULER", True),
            run_once=_bool("RUN_ONCE", False),
            schedule=Schedule(
                enabled=True,
                hour=hour,
                minute=minute,
                jitter_minutes=jitter,
                # 优先读 HUOHUA_TZ（显式覆盖），回退到系统标准变量 TZ ——
                # Docker / systemd 都是直接设 TZ 的，不能只认带前缀的那个
                timezone=_raw("TZ") or _raw_unprefixed("TZ") or "Asia/Shanghai",
            ),
        )

    def describe(self) -> str:
        if not self.enabled:
            return "内置调度器已关闭（由外部 cron 触发）"
        return self.schedule.describe()


@dataclass(frozen=True, slots=True)
class WorkbenchSettings:
    """工作台相关。"""

    host: str = "0.0.0.0"
    port: int = 8787
    token: str = ""
    allowed_ips: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> WorkbenchSettings:
        return cls(
            host=_str("HOST", "0.0.0.0"),
            port=_int("PORT", 8787, minimum=1, maximum=65535),
            token=_str("TOKEN"),
            allowed_ips=_csv("ALLOWED_IPS"),
        )

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.token:
            problems.append("HUOHUA_TOKEN 未设置 —— 工作台将对任何能访问端口的人开放")
        elif len(self.token) < 12:
            problems.append(f"HUOHUA_TOKEN 过短（{len(self.token)} 字符），建议至少 24 位")

        if self.allowed_ips:
            import ipaddress

            for item in self.allowed_ips:
                try:
                    if "/" in item:
                        ipaddress.ip_network(item, strict=False)
                    else:
                        ipaddress.ip_address(item)
                except ValueError:
                    problems.append(f"HUOHUA_ALLOWED_IPS 格式错误：{item!r}")
        return problems

    def describe(self) -> str:
        guard = []
        if self.token:
            guard.append("令牌鉴权")
        if self.allowed_ips:
            guard.append(f"IP 白名单({len(self.allowed_ips)})")
        return f"http://{self.host}:{self.port}｜{' + '.join(guard) if guard else '⚠ 无任何防护'}"


# =============================================================================
# 顶层设置
# =============================================================================


@dataclass(frozen=True, slots=True)
class Settings:
    """全部配置。一次性从环境读取，之后不可变。"""

    browser: BrowserSettings
    send: SendSettings
    notify: NotifySettings
    scheduler: SchedulerSettings
    workbench: WorkbenchSettings
    data_dir: Path
    log_dir: Path
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            browser=BrowserSettings.from_env(),
            send=SendSettings.from_env(),
            notify=NotifySettings.from_env(),
            scheduler=SchedulerSettings.from_env(),
            workbench=WorkbenchSettings.from_env(),
            data_dir=Path(_str("DATA_DIR", "data")),
            log_dir=Path(_str("LOG_DIR", "logs")),
            log_level=_str("LOG_LEVEL", "INFO").upper(),
        )

    # --- 派生属性 -----------------------------------------------------------

    @property
    def accounts_dir(self) -> Path:
        return self.data_dir / "accounts"

    @property
    def config_dir(self) -> Path:
        return self.data_dir / "config"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    def ensure_dirs(self) -> None:
        """创建数据目录。容器入口脚本也会做一遍，这里是兜底。"""
        for directory in (self.data_dir, self.accounts_dir, self.config_dir, self.runs_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)

    # --- 校验与展示 ---------------------------------------------------------

    def validate(self) -> list[str]:
        """汇总所有配置问题。返回值不抛异常，让调用方决定怎么处理。"""
        problems = [*self.notify.validate(), *self.workbench.validate()]

        if (
            not self.browser.headless
            and os.name != "nt"
            and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        ):
            problems.append(
                "HUOHUA_HEADLESS=false 但当前环境没有 DISPLAY —— 容器/服务器上请设为 true，或安装 Xvfb"
            )

        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            problems.append(f"HUOHUA_LOG_LEVEL 取值不合法：{self.log_level!r}")

        return problems

    def describe(self) -> str:
        lines = [
            "douyin-huohua-keeper 配置",
            "=" * 60,
            "",
            "  工作台      " + self.workbench.describe(),
            "  浏览器      " + self.browser.describe(),
            "  发送        " + self.send.describe(),
            "  调度        " + self.scheduler.describe(),
            "  通知        " + self.notify.describe(),
            "",
            f"  数据目录    {self.data_dir.resolve()}",
            f"  日志目录    {self.log_dir.resolve()}",
            f"  日志级别    {self.log_level}",
            "",
            "=" * 60,
        ]

        problems = self.validate()
        if problems:
            lines.append(f"发现 {len(problems)} 个配置问题：")
            lines.extend(f"  ⚠ {p}" for p in problems)
        else:
            lines.append("配置校验通过")
        return "\n".join(lines)


# =============================================================================
# 单例
# =============================================================================

_settings: Settings | None = None


def load_settings(*, refresh: bool = False) -> Settings:
    """读取配置。

    默认缓存 —— 配置在进程生命周期内不变，重复读取没有意义。
    测试里用 ``refresh=True`` 强制重读环境变量。
    """
    global _settings
    if _settings is None or refresh:
        _settings = Settings.from_env()
    return _settings


def reset_settings() -> None:
    """清掉缓存。测试专用。"""
    global _settings
    _settings = None
