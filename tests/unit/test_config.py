"""配置层测试。

重点：每种取值的解析都要对，非法值必须报错而不是静默用默认值 ——
静默用默认值会让「我明明设了」和「实际生效的」不一致，这是最难查的一类问题。
"""

from __future__ import annotations

import pytest

from douyin_huohua_keeper.config import (
    BrowserSettings,
    ConfigError,
    NotifySettings,
    SchedulerSettings,
    SendSettings,
    Settings,
    WorkbenchSettings,
    _parse_resource_types,
    _parse_sandbox_mode,
    load_settings,
    reset_settings,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch):
    """清掉所有 HUOHUA_ 变量和 TZ，让每个测试从干净的起点开始。"""
    import os

    for key in list(os.environ):
        if key.startswith("HUOHUA_") or key == "TZ":
            monkeypatch.delenv(key, raising=False)
    reset_settings()
    yield
    reset_settings()


class TestDefaults:
    def test_browser_defaults(self) -> None:
        browser = BrowserSettings.from_env()

        assert browser.headless is True
        assert browser.nav_timeout_ms == 45_000
        assert browser.slow_mo_ms == 0

    def test_send_defaults(self) -> None:
        send = SendSettings.from_env()

        assert send.confirm_timeout_ms == 15_000
        assert send.max_retries == 3
        assert send.gap.minimum == 20.0
        assert send.gap.maximum == 90.0
        assert send.dry_run is False

    def test_notify_defaults(self) -> None:
        notify = NotifySettings.from_env()

        assert notify.channels == ()
        assert notify.enabled() is False
        assert notify.warn_threshold == 2
        assert notify.critical_threshold == 3

    def test_scheduler_defaults(self) -> None:
        scheduler = SchedulerSettings.from_env()

        assert scheduler.enabled is True
        assert scheduler.schedule.hour == 10
        assert scheduler.schedule.minute == 30
        assert scheduler.schedule.jitter_minutes == 25

    def test_workbench_defaults(self) -> None:
        workbench = WorkbenchSettings.from_env()

        assert workbench.port == 8787
        assert workbench.token == ""
        assert workbench.allowed_ips == ()


class TestBrowserParsing:
    def test_headless_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_HEADLESS", "false")

        assert BrowserSettings.from_env().headless is False

    @pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "TRUE", "True"])
    def test_truthy_variants(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("HUOHUA_HEADLESS", raw)

        assert BrowserSettings.from_env().headless is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", "FALSE"])
    def test_falsy_variants(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv("HUOHUA_HEADLESS", raw)

        assert BrowserSettings.from_env().headless is False

    def test_invalid_bool_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """拼错的值必须报错 —— 静默当成 true 会让人困惑为什么改不生效。"""
        monkeypatch.setenv("HUOHUA_HEADLESS", "maybe")

        with pytest.raises(ConfigError, match="HUOHUA_HEADLESS"):
            BrowserSettings.from_env()

    def test_custom_browser_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_BROWSER_PATH", "/usr/bin/chromium")

        path = BrowserSettings.from_env().browser_path
        assert path is not None
        assert str(path).endswith("chromium")

    def test_timeout_below_minimum_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_NAV_TIMEOUT_MS", "100")

        with pytest.raises(ConfigError, match="NAV_TIMEOUT_MS"):
            BrowserSettings.from_env()

    def test_invalid_int_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_SLOW_MO_MS", "fast")

        with pytest.raises(ConfigError, match="必须是整数"):
            BrowserSettings.from_env()


class TestSendParsing:
    def test_gap_reversed_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """max < min 是明显的配置错误，要说清楚。"""
        monkeypatch.setenv("HUOHUA_SEND_GAP_MIN_SEC", "60")
        monkeypatch.setenv("HUOHUA_SEND_GAP_MAX_SEC", "10")

        with pytest.raises(ConfigError, match="SEND_GAP_MAX_SEC"):
            SendSettings.from_env()

    def test_max_retries_zero_is_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_MAX_RETRIES", "0")

        assert SendSettings.from_env().max_retries == 0

    def test_max_retries_too_large_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_MAX_RETRIES", "99")

        with pytest.raises(ConfigError, match="MAX_RETRIES"):
            SendSettings.from_env()

    def test_dry_run_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_DRY_RUN", "true")

        assert SendSettings.from_env().dry_run is True

    def test_describe_mentions_dry_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_DRY_RUN", "true")

        assert "演练" in SendSettings.from_env().describe()


class TestNotifyParsing:
    def test_channels_parsed_and_normalized(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_NOTIFY_CHANNELS", " Bark , ServerChan ,bark")

        channels = NotifySettings.from_env().channels
        # 大小写归一到小写，重复项保留（去重不是这一步的职责）
        assert "bark" in channels
        assert "serverchan" in channels

    def test_empty_channels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_NOTIFY_CHANNELS", "  ,  ,")

        assert NotifySettings.from_env().channels == ()

    def test_unknown_channel_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_NOTIFY_CHANNELS", "bark,telepathy")
        monkeypatch.setenv("HUOHUA_BARK_URL", "https://api.day.app/key")

        problems = NotifySettings.from_env().validate()
        assert any("telepathy" in p for p in problems)

    def test_missing_channel_config_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_NOTIFY_CHANNELS", "telegram")

        problems = NotifySettings.from_env().validate()
        assert len(problems) == 1
        assert "TELEGRAM_BOT_TOKEN" in problems[0]
        assert "TELEGRAM_CHAT_ID" in problems[0]

    def test_complete_channel_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_NOTIFY_CHANNELS", "bark")
        monkeypatch.setenv("HUOHUA_BARK_URL", "https://api.day.app/abc")

        assert NotifySettings.from_env().validate() == []

    def test_dingtalk_requires_secret_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """钉钉只填 webhook 不填 secret 是常见错误 —— 要指出缺哪个。"""
        monkeypatch.setenv("HUOHUA_NOTIFY_CHANNELS", "dingtalk")
        monkeypatch.setenv("HUOHUA_DINGTALK_WEBHOOK", "https://oapi.dingtalk.com/robot/send?access_token=x")

        problems = NotifySettings.from_env().validate()
        assert any("DINGTALK_SECRET" in p for p in problems)

    def test_warn_threshold_must_be_below_critical(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """warn >= critical 会导致永远升不到 CRITICAL，必须拦住。"""
        monkeypatch.setenv("HUOHUA_WARN_THRESHOLD", "5")
        monkeypatch.setenv("HUOHUA_CRITICAL_THRESHOLD", "3")

        with pytest.raises(ConfigError, match="CRITICAL_THRESHOLD"):
            NotifySettings.from_env()

    def test_equal_thresholds_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_WARN_THRESHOLD", "3")
        monkeypatch.setenv("HUOHUA_CRITICAL_THRESHOLD", "3")

        with pytest.raises(ConfigError):
            NotifySettings.from_env()

    def test_describe_without_channels(self) -> None:
        assert "未启用" in NotifySettings.from_env().describe()

    def test_describe_with_channels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_NOTIFY_CHANNELS", "bark,feishu")

        describe = NotifySettings.from_env().describe()
        assert "bark" in describe
        assert "feishu" in describe


class TestSchedulerParsing:
    def test_custom_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_SCHEDULE_HOUR", "21")
        monkeypatch.setenv("HUOHUA_SCHEDULE_MINUTE", "5")
        monkeypatch.setenv("HUOHUA_JITTER_MINUTES", "0")

        schedule = SchedulerSettings.from_env().schedule
        assert schedule.hour == 21
        assert schedule.minute == 5
        assert schedule.jitter_minutes == 0

    def test_hour_out_of_range_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_SCHEDULE_HOUR", "25")

        with pytest.raises(ConfigError, match="SCHEDULE_HOUR"):
            SchedulerSettings.from_env()

    def test_disabled_scheduler(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_ENABLE_SCHEDULER", "false")

        settings = SchedulerSettings.from_env()
        assert settings.enabled is False
        assert "关闭" in settings.describe()

    def test_timezone_from_tz(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TZ 要被读进调度配置 —— 火花按自然日算，时区错了发送窗口就错了。

        注意 Windows 上 ``os.environ`` 大小写不敏感，直接断言具体值会不稳，
        这里只断言「读到了一个非空时区」且「不是硬编码的默认值」。
        """
        monkeypatch.setenv("TZ", "America/New_York")

        timezone = SchedulerSettings.from_env().schedule.timezone
        assert timezone == "America/New_York"

    def test_timezone_defaults_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TZ", raising=False)

        assert SchedulerSettings.from_env().schedule.timezone == "Asia/Shanghai"


class TestWorkbenchParsing:
    def test_short_token_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_TOKEN", "abc")

        problems = WorkbenchSettings.from_env().validate()
        assert any("过短" in p for p in problems)

    def test_missing_token_reported(self) -> None:
        problems = WorkbenchSettings.from_env().validate()
        assert any("未设置" in p for p in problems)

    def test_good_token_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_TOKEN", "a" * 32)

        assert not any("TOKEN" in p for p in WorkbenchSettings.from_env().validate())

    def test_valid_cidr_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_ALLOWED_IPS", "203.0.113.7,198.51.100.0/24")
        monkeypatch.setenv("HUOHUA_TOKEN", "a" * 32)

        assert WorkbenchSettings.from_env().validate() == []

    def test_invalid_ip_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_ALLOWED_IPS", "not-an-ip")

        problems = WorkbenchSettings.from_env().validate()
        assert any("not-an-ip" in p for p in problems)

    def test_ipv6_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_ALLOWED_IPS", "2001:db8::/32,::1")
        monkeypatch.setenv("HUOHUA_TOKEN", "a" * 32)

        assert WorkbenchSettings.from_env().validate() == []

    def test_describe_shows_guards(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_TOKEN", "x" * 32)
        monkeypatch.setenv("HUOHUA_ALLOWED_IPS", "10.0.0.1")

        describe = WorkbenchSettings.from_env().describe()
        assert "令牌鉴权" in describe
        assert "IP 白名单" in describe

    def test_describe_warns_when_unguarded(self) -> None:
        assert "无任何防护" in WorkbenchSettings.from_env().describe()


class TestSettingsAggregate:
    def test_derived_paths(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setenv("HUOHUA_DATA_DIR", str(tmp_path / "mydata"))

        settings = Settings.from_env()
        assert settings.accounts_dir == tmp_path / "mydata" / "accounts"
        assert settings.config_dir == tmp_path / "mydata" / "config"
        assert settings.runs_dir == tmp_path / "mydata" / "runs"

    def test_ensure_dirs_creates_everything(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setenv("HUOHUA_DATA_DIR", str(tmp_path / "d"))
        monkeypatch.setenv("HUOHUA_LOG_DIR", str(tmp_path / "l"))

        settings = Settings.from_env()
        settings.ensure_dirs()

        assert settings.accounts_dir.is_dir()
        assert settings.config_dir.is_dir()
        assert settings.runs_dir.is_dir()
        assert settings.log_dir.is_dir()

    def test_bad_log_level_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_LOG_LEVEL", "VERBOSE")

        problems = Settings.from_env().validate()
        assert any("LOG_LEVEL" in p for p in problems)

    def test_describe_contains_all_sections(self) -> None:
        describe = Settings.from_env().describe()

        for section in ("工作台", "浏览器", "发送", "调度", "通知", "数据目录"):
            assert section in describe

    def test_describe_reports_problems(self) -> None:
        """没有 token 时 describe 应该明确列出来。"""
        describe = Settings.from_env().describe()
        assert "配置问题" in describe
        assert "TOKEN" in describe


class TestSingleton:
    def test_load_settings_caches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_PORT", "1111")

        first = load_settings(refresh=True)
        monkeypatch.setenv("HUOHUA_PORT", "2222")
        second = load_settings()

        assert first is second
        assert first.workbench.port == 1111

    def test_refresh_rereads_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_PORT", "1111")
        load_settings(refresh=True)

        monkeypatch.setenv("HUOHUA_PORT", "2222")
        refreshed = load_settings(refresh=True)

        assert refreshed.workbench.port == 2222

    def test_reset_clears_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_PORT", "1111")
        first = load_settings(refresh=True)

        reset_settings()
        monkeypatch.setenv("HUOHUA_PORT", "2222")
        second = load_settings()

        assert second is not first
        assert second.workbench.port == 2222


class TestHeadlessDisplayCheck:
    def test_headless_false_without_display_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """容器里设了有头模式但没 DISPLAY —— 启动必失败，必须提前拦。"""
        import os

        if os.name == "nt":
            pytest.skip("Windows 上不检查 DISPLAY")

        monkeypatch.setenv("HUOHUA_HEADLESS", "false")
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

        problems = Settings.from_env().validate()
        assert any("DISPLAY" in p for p in problems)

    def test_headless_true_never_complains(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HUOHUA_HEADLESS", "true")

        problems = Settings.from_env().validate()
        assert not any("DISPLAY" in p for p in problems)


class TestParseResourceTypes:
    """`HUOHUA_BLOCK_RESOURCE_TYPES` 的解析（省流量优化，见 browser._install_resource_blocker）。"""

    def test_default_is_font_and_media(self) -> None:
        assert _parse_resource_types("font,media") == ("font", "media")

    def test_sorted_and_deduped(self) -> None:
        assert _parse_resource_types(" media , font ,media") == ("font", "media")

    @pytest.mark.parametrize("raw", ["none", "off", "false", "0", "", "  "])
    def test_disable_sentinels(self, raw: str) -> None:
        """填 none/off 表示完全不拦 —— 排障时先关掉它能排除拦截嫌疑。"""
        assert _parse_resource_types(raw) == ()

    def test_browser_settings_reads_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from douyin_huohua_keeper.config import BrowserSettings

        monkeypatch.setenv("HUOHUA_BLOCK_RESOURCE_TYPES", "media,font,image")
        settings = BrowserSettings.from_env()
        assert settings.blocked_resource_types == ("font", "image", "media")

        monkeypatch.setenv("HUOHUA_BLOCK_RESOURCE_TYPES", "none")
        assert BrowserSettings.from_env().blocked_resource_types == ()


class TestParseSandboxMode:
    """`HUOHUA_CHROMIUM_NO_SANDBOX` 的解析。写错必须报错，不能默默当成 auto。"""

    @pytest.mark.parametrize("raw", ["auto", "AUTO", " auto ", "always", "never", ""])
    def test_accepts_known_values(self, raw: str) -> None:
        assert _parse_sandbox_mode(raw) in ("auto", "always", "never")

    @pytest.mark.parametrize("raw", ["yes", "true", "关闭", "1"])
    def test_rejects_unknown_values(self, raw: str) -> None:
        with pytest.raises(Exception):  # noqa: B017
            _parse_sandbox_mode(raw)

    def test_default_is_auto(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from douyin_huohua_keeper.config import BrowserSettings

        monkeypatch.delenv("HUOHUA_CHROMIUM_NO_SANDBOX", raising=False)
        assert BrowserSettings.from_env().chromium_no_sandbox == "auto"
