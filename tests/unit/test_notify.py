"""通知系统测试。

不打真实网络。通道的 HTTP 层用替身，测的是：
- 分级过滤逻辑（不该推的不推，该推的一定推）
- 失败重试与「全部失败」的显式记录
- 通道配置校验（只填一半的配置要能被发现）
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from douyin_huohua_keeper.models import AlertLevel, FailureKind, RunReport, RunStatus, TargetOutcome
from douyin_huohua_keeper.notify.channels import (
    BarkChannel,
    Channel,
    DeliveryResult,
    DingTalkChannel,
    FeishuChannel,
    GenericWebhookChannel,
    ServerChanChannel,
    TelegramChannel,
    build_channel,
    build_channels,
)
from douyin_huohua_keeper.notify.dispatcher import (
    Alert,
    Dispatcher,
    level_from_state,
)


class FakeChannel(Channel):
    """可编程的通道替身。"""

    def __init__(self, name: str, *, results: list[bool] | None = None) -> None:
        self.name = name
        self._results = list(results or [True])
        self.calls: list[tuple[AlertLevel, str, str]] = []

    def validate_config(self) -> list[str]:
        return []

    def send(self, level: AlertLevel, title: str, body: str, *, timeout: float = 10.0) -> DeliveryResult:
        self.calls.append((level, title, body))
        ok = self._results.pop(0) if self._results else self._results_last
        self._results_last = ok
        return DeliveryResult(channel=self.name, ok=ok, detail="ok" if ok else "boom")

    _results_last: bool = True


class TestChannelConfigValidation:
    def test_bark_requires_url(self) -> None:
        assert BarkChannel("").validate_config() == ["HUOHUA_BARK_URL"]
        assert BarkChannel("https://api.day.app/key").validate_config() == []

    def test_serverchan_requires_key(self) -> None:
        assert ServerChanChannel("").validate_config() == ["HUOHUA_SERVERCHAN_KEY"]

    def test_dingtalk_requires_both(self) -> None:
        """只填 webhook 不填 secret 是群里最常见的配置错误。"""
        channel = DingTalkChannel("https://oapi.dingtalk.com/robot/send?access_token=x", "")

        missing = channel.validate_config()
        assert missing == ["HUOHUA_DINGTALK_SECRET"]

    def test_dingtalk_complete(self) -> None:
        channel = DingTalkChannel("https://example.com/hook", "SECxxx")
        assert channel.validate_config() == []

    def test_feishu_requires_webhook(self) -> None:
        assert FeishuChannel("").validate_config() == ["HUOHUA_FEISHU_WEBHOOK"]

    def test_telegram_requires_both(self) -> None:
        assert TelegramChannel("", "").validate_config() == [
            "HUOHUA_TELEGRAM_BOT_TOKEN",
            "HUOHUA_TELEGRAM_CHAT_ID",
        ]
        assert TelegramChannel("token", "").validate_config() == ["HUOHUA_TELEGRAM_CHAT_ID"]

    def test_generic_webhook_requires_url(self) -> None:
        assert GenericWebhookChannel("").validate_config() == ["HUOHUA_GENERIC_WEBHOOK"]


class TestBuildChannels:
    def _settings(self, **kwargs) -> object:
        defaults = {
            "channels": (),
            "bark_url": "",
            "serverchan_key": "",
            "dingtalk_webhook": "",
            "dingtalk_secret": "",
            "feishu_webhook": "",
            "telegram_bot_token": "",
            "telegram_chat_id": "",
            "generic_webhook": "",
        }
        defaults.update(kwargs)
        return type("S", (), defaults)()

    def test_builds_configured_channel(self) -> None:
        settings = self._settings(channels=("bark",), bark_url="https://api.day.app/k")

        channels = build_channels(settings)
        assert len(channels) == 1
        assert channels[0].name == "bark"

    def test_skips_incomplete_channel(self) -> None:
        """配置不完整的通道要被跳过并记日志，而不是启动时崩掉。

        理由：一个通道配错了不该让整个服务起不来 —— 你还能通过
        工作台看到问题并修正。
        """
        settings = self._settings(channels=("bark", "telegram"), bark_url="https://api.day.app/k")

        channels = build_channels(settings)
        assert [c.name for c in channels] == ["bark"]

    def test_unknown_channel_returns_none(self) -> None:
        assert build_channel("carrier-pigeon", self._settings()) is None

    def test_empty_config_builds_nothing(self) -> None:
        assert build_channels(self._settings()) == ()


class TestLevelRouting:
    def test_normal_is_silent_by_default(self) -> None:
        channel = FakeChannel("test")
        dispatcher = Dispatcher((channel,))

        report = dispatcher.dispatch(Alert(level=AlertLevel.NORMAL, title="t", body="b"))

        assert report.attempted == ()
        assert channel.calls == []
        assert "默认不推送" in report.skipped_reason

    def test_notice_is_pushed(self) -> None:
        """NOTICE **必须推** —— 它代表「这一轮真的有人没发出去」。

        这条改过一次，背景是一次真实的静默失败：以前 NOTICE 静默，
        而「连续失败」只在跨天累计，于是「今天有 3 个人没收到消息」
        要等到第二天又失败才会被告知 —— 用户连着几天没发现火花断了。
        用户的原话是「失败两天了才提示」。
        """
        channel = FakeChannel("test")
        dispatcher = Dispatcher((channel,))

        report = dispatcher.dispatch(Alert(level=AlertLevel.NOTICE, title="t", body="b"))

        assert len(channel.calls) == 1
        assert report.any_succeeded is True

    @pytest.mark.parametrize(
        "level",
        [AlertLevel.WARNING, AlertLevel.CRITICAL, AlertLevel.RISK],
    )
    def test_important_levels_are_pushed(self, level: AlertLevel) -> None:
        channel = FakeChannel("test")
        dispatcher = Dispatcher((channel,))

        report = dispatcher.dispatch(Alert(level=level, title="t", body="b"))

        assert len(channel.calls) == 1
        assert report.any_succeeded is True

    def test_minimum_level_filter(self) -> None:
        channel = FakeChannel("test")
        dispatcher = Dispatcher((channel,))

        report = dispatcher.dispatch(
            Alert(level=AlertLevel.NOTICE, title="t", body="b"),
            minimum_level=AlertLevel.WARNING,
        )

        assert report.attempted == ()
        assert "低于推送门槛" in report.skipped_reason

    def test_minimum_level_allows_forced_notice(self) -> None:
        channel = FakeChannel("test")
        dispatcher = Dispatcher((channel,))

        dispatcher.dispatch(
            Alert(level=AlertLevel.NOTICE, title="t", body="b"),
            minimum_level=AlertLevel.NOTICE,
        )

        assert len(channel.calls) == 1

    def test_no_channels_reports_reason(self) -> None:
        dispatcher = Dispatcher(())

        report = dispatcher.dispatch(Alert(level=AlertLevel.CRITICAL, title="t", body="b"))

        assert report.attempted == ()
        assert "未配置" in report.skipped_reason


class TestDeliveryRetry:
    def test_retries_on_failure(self) -> None:
        """通知本身也会失败 —— 网络抖动、对方限流。要重试。"""
        channel = FakeChannel("flaky", results=[False, False, True])
        dispatcher = Dispatcher((channel,), retries=2)

        report = dispatcher.dispatch(Alert(level=AlertLevel.CRITICAL, title="t", body="b"))

        assert report.any_succeeded is True
        assert len(channel.calls) == 3

    def test_gives_up_after_retries(self) -> None:
        channel = FakeChannel("broken", results=[False, False, False])
        dispatcher = Dispatcher((channel,), retries=2)

        report = dispatcher.dispatch(Alert(level=AlertLevel.CRITICAL, title="t", body="b"))

        assert report.any_succeeded is False
        assert report.failed_channels == ("broken",)
        assert len(channel.calls) == 3

    def test_reports_which_channels_failed(self) -> None:
        """多通道时要说清楚哪些成功哪些失败。"""
        good = FakeChannel("good", results=[True])
        bad = FakeChannel("bad", results=[False, False, False])
        dispatcher = Dispatcher((good, bad), retries=2)

        report = dispatcher.dispatch(Alert(level=AlertLevel.CRITICAL, title="t", body="b"))

        assert report.any_succeeded is True
        assert report.succeeded_channels == ("good",)
        assert report.failed_channels == ("bad",)

    def test_zero_retries_tries_once(self) -> None:
        channel = FakeChannel("once", results=[False])
        dispatcher = Dispatcher((channel,), retries=0)

        dispatcher.dispatch(Alert(level=AlertLevel.CRITICAL, title="t", body="b"))

        assert len(channel.calls) == 1

    def test_summary_text(self) -> None:
        channel = FakeChannel("bark", results=[True])
        dispatcher = Dispatcher((channel,))

        report = dispatcher.dispatch(Alert(level=AlertLevel.WARNING, title="t", body="b"))

        assert "bark" in report.summary()
        assert "已推送" in report.summary()


class TestLevelFromState:
    def test_all_good_is_normal(self) -> None:
        assert level_from_state(consecutive_failures=0) is AlertLevel.NORMAL

    def test_single_failure_is_notice(self) -> None:
        assert level_from_state(consecutive_failures=1) is AlertLevel.NOTICE

    def test_warning_threshold(self) -> None:
        assert level_from_state(consecutive_failures=2, warn_threshold=2) is AlertLevel.WARNING

    def test_critical_threshold(self) -> None:
        assert level_from_state(consecutive_failures=3, critical_threshold=3) is AlertLevel.CRITICAL

    def test_beyond_critical_stays_critical(self) -> None:
        assert level_from_state(consecutive_failures=99, critical_threshold=3) is AlertLevel.CRITICAL

    def test_auth_expiry_is_risk(self) -> None:
        """登录态失效要立刻叫人，不管连续失败计数是多少。"""
        assert level_from_state(consecutive_failures=0, auth_expired=True) is AlertLevel.RISK

    def test_risk_detected_is_risk(self) -> None:
        assert level_from_state(consecutive_failures=0, risk_detected=True) is AlertLevel.RISK

    def test_risk_beats_auth(self) -> None:
        """风控优先于认证 —— 两者都需要立刻行动，但风控的处理方式更特殊。"""
        level = level_from_state(consecutive_failures=5, auth_expired=True, risk_detected=True)
        assert level is AlertLevel.RISK

    def test_disabled_warn_threshold_still_allows_critical(self) -> None:
        """warn_threshold=0 表示不发 WARNING，但 CRITICAL 仍然生效 ——
        不能因为关掉了中间档就把严重情况一起吞掉。"""
        level = level_from_state(consecutive_failures=10, warn_threshold=0, critical_threshold=3)
        assert level is AlertLevel.CRITICAL

    def test_both_thresholds_disabled_gives_notice(self) -> None:
        """两个阈值都关掉时，有失败只说 NOTICE，不升级。"""
        level = level_from_state(
            consecutive_failures=99,
            warn_threshold=0,
            critical_threshold=0,
        )
        assert level is AlertLevel.NOTICE

    def test_custom_thresholds(self) -> None:
        """阈值可配：warn=5 / critical=10 时，5 次应该只到 WARNING。"""
        assert (
            level_from_state(consecutive_failures=5, warn_threshold=5, critical_threshold=10)
            is AlertLevel.WARNING
        )
        assert (
            level_from_state(consecutive_failures=9, warn_threshold=5, critical_threshold=10)
            is AlertLevel.WARNING
        )
        assert (
            level_from_state(consecutive_failures=10, warn_threshold=5, critical_threshold=10)
            is AlertLevel.CRITICAL
        )

    def test_failure_takes_precedence_over_thresholds(self) -> None:
        """认证失效时即使失败次数没到阈值，也要报 RISK。"""
        level = level_from_state(
            consecutive_failures=1,
            auth_expired=True,
            warn_threshold=5,
            critical_threshold=10,
        )
        assert level is AlertLevel.RISK

    def test_failed_now_triggers_notice_without_streak(self) -> None:
        """★ 核心回归：「今天先成功过、之后全失败」也必须报警。

        ``consecutive_failures`` 按天累计，同一天里「先成功后失败」不会加一。
        于是会出现：早上全失败(计 1 天) → 手动重试成功(归零) → 晚上 15 人全失败
        ⇒ 计数仍是 0 ⇒ 级别 NORMAL ⇒ **一条通知都不发**。
        用户以为一切正常，实际当天最后一轮一个人都没发出去。
        """
        level = level_from_state(consecutive_failures=0, failed_now=15)

        assert level is AlertLevel.NOTICE

    def test_uncertain_now_triggers_notice(self) -> None:
        """「结果不确定」也要说一声 —— 它同样意味着可能有人没收到。"""
        assert level_from_state(consecutive_failures=0, uncertain_now=2) is AlertLevel.NOTICE

    def test_no_failures_stays_normal(self) -> None:
        assert level_from_state(consecutive_failures=0, failed_now=0, uncertain_now=0) is AlertLevel.NORMAL

    def test_failed_now_does_not_override_streak_level(self) -> None:
        """本轮失败不该把已经达标的严重级别降下来。"""
        level = level_from_state(consecutive_failures=3, failed_now=1, critical_threshold=3)

        assert level is AlertLevel.CRITICAL


class TestAlert:
    def test_frozen(self) -> None:
        alert = Alert(level=AlertLevel.WARNING, title="t", body="b")

        # frozen dataclass 赋值抛 FrozenInstanceError（AttributeError 的子类）
        with pytest.raises(FrozenInstanceError):
            alert.title = "changed"  # type: ignore[misc]

    def test_context_default_empty(self) -> None:
        assert Alert(level=AlertLevel.WARNING, title="t", body="b").context == {}


class TestUrlEscaping:
    """通知 URL 里的标题/正文必须转义，否则空格、&、/ 会破坏请求。

    标题和正文都来自告警文案，里面有中文、空格、`&`、`/`、`?` 都是常态。
    """

    @staticmethod
    def _capture_bark_url(monkeypatch: pytest.MonkeyPatch, level: AlertLevel, title: str, body: str) -> str:
        channel = BarkChannel("https://api.day.app/testkey")
        captured: dict[str, str] = {}

        def fake_get(url: str, timeout: float = 10.0) -> DeliveryResult:
            captured["url"] = url
            return DeliveryResult(channel=channel.name, ok=True, detail="")

        monkeypatch.setattr(channel, "_get", fake_get)
        result = channel.send(level, title, body)
        assert result.ok
        return captured["url"]

    def test_bark_escapes_special_characters(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from urllib.parse import unquote, urlsplit

        url = self._capture_bark_url(monkeypatch, AlertLevel.NORMAL, "标题 空格&符号", "正文/斜杠?问号")

        parts = urlsplit(url)
        segments = parts.path.split("/")
        # 路径形如 /<key>/<title>/<body>，反转义后应与原文一致
        assert unquote(segments[1]) == "testkey"
        assert unquote(segments[2]) == "标题 空格&符号"
        assert unquote(segments[3]) == "正文/斜杠?问号"
        # 原始 URL 里这些字符不应裸露出现（除查询串的分隔符外）
        assert " " not in parts.path

    def test_bark_normal_level_is_quiet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from urllib.parse import parse_qs, urlsplit

        url = self._capture_bark_url(monkeypatch, AlertLevel.NORMAL, "t", "b")
        query = parse_qs(urlsplit(url).query)
        assert query["level"] == ["active"]
        assert "sound" not in query
        assert "call" not in query

    def test_bark_urgent_level_uses_alarm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from urllib.parse import parse_qs, urlsplit

        url = self._capture_bark_url(monkeypatch, AlertLevel.CRITICAL, "t", "b")
        query = parse_qs(urlsplit(url).query)
        assert query["level"] == ["critical"]
        assert query["sound"] == ["alarm"]
        assert query["call"] == ["1"]


class TestTestNotification:
    def test_send_test_uses_warning_level(self) -> None:
        """测试通知要用会被推送的级别，否则按钮点了没反应。"""
        channel = FakeChannel("test")
        dispatcher = Dispatcher((channel,), retries=0)

        report = dispatcher.send_test()

        assert report.any_succeeded is True
        assert len(channel.calls) == 1
        level, title, body = channel.calls[0]
        assert level is AlertLevel.WARNING
        assert "测试" in title
        assert "配置正确" in body


class TestAlertTexts:
    """通知文案必须能一眼分清三件事：登录态失效 / 发送失败 / 结果无法确认。

    背景：用户明确要求 —— 登录态真失效时推送要写「登录态已过期」，
    不能含糊成「有人发送结果不确定」，而且两者绝不能混。
    """

    def _report(self, statuses: list[RunStatus], *, failed_kind: FailureKind | None = None) -> RunReport:
        outcomes = tuple(
            TargetOutcome(
                name=f"好友{i}",
                status=status,
                failure_kind=failed_kind if status is RunStatus.FAILED else None,
                detail="测试用",
            )
            for i, status in enumerate(statuses, 1)
        )
        return RunReport(
            run_id="20260101-000000-test",
            account_id="main",
            started_at="2026-01-01T00:00:00",
            finished_at="2026-01-01T00:00:10",
            dry_run=False,
            outcomes=outcomes,
        )

    def _craft(self, report: RunReport, **kwargs):
        from douyin_huohua_keeper.scheduler.runner import _craft_alert_text

        params = {
            "report": report,
            "level": AlertLevel.NOTICE,
            "abort_reason": None,
            "auth_expired": False,
            "risk_detected": False,
        }
        params.update(kwargs)
        return _craft_alert_text(**params)

    def test_auth_expired_says_so_explicitly(self) -> None:
        title, body = self._craft(
            self._report([RunStatus.FAILED] * 3, failed_kind=FailureKind.AUTH),
            auth_expired=True,
            abort_reason="登录态已过期（约 1 天前失效），需要重新扫码",
            failed_now=3,
        )

        assert "登录态已过期" in title
        assert "重新扫码" in title
        # 必须和「无法确认」划清界限
        assert "无法确认" in body

    def test_all_failed_points_at_login_first(self) -> None:
        """一个都没成功：标题要说清严重性，正文把登录态放在第一个排查项。"""
        title, body = self._craft(
            self._report([RunStatus.FAILED] * 5, failed_kind=FailureKind.TRANSIENT),
            failed_now=5,
        )

        assert "一个都没发出去" in title
        assert "登录态" in body
        assert "立即发送一次" in body

    def test_uncertain_only_is_not_described_as_failure(self) -> None:
        """只有「无法确认」时，绝不能让人以为是发送失败或登录态问题。"""
        title, body = self._craft(
            self._report([RunStatus.SUCCESS, RunStatus.SUCCESS, RunStatus.UNCERTAIN, RunStatus.UNCERTAIN]),
            uncertain_now=2,
            failed_now=0,
        )

        assert "无法确认" in title
        assert "这不是发送失败" in body
        assert "不代表登录态失效" in body
        # 不能出现「失败」这种会吓人的措辞当标题
        assert "没发出去" not in title

    def test_partial_failure_lists_who_failed(self) -> None:
        title, body = self._craft(
            self._report([RunStatus.SUCCESS, RunStatus.FAILED, RunStatus.FAILED]),
            failed_now=2,
        )

        assert "2 人没发出去" in title
        assert "好友2" in body and "好友3" in body
