"""错误分类与重试决策测试。

这块逻辑决定「失败了之后干什么」，判错了会直接造成损失：
- 把 AUTH 当成 TRANSIENT → 无意义地重试，加重风控
- 把 PERMANENT 当成 TRANSIENT → 白等三轮退避，火花可能因此断掉
- 把 RISK 当成普通错误 → 继续猛发，把账号搞限制
"""

from __future__ import annotations

import pytest

from douyin_huohua_keeper.common.errors import (
    ABORT_TASK,
    NON_RETRYABLE,
    RetryBudget,
    classify,
    classify_exception,
    decide_retry,
    summarize_failures,
)
from douyin_huohua_keeper.models import FailureKind, RunStatus, TargetOutcome


class TestClassify:
    @pytest.mark.parametrize(
        "text",
        [
            "登录态已失效，需要重新扫码",
            "请先登录",
            "被重定向到登录页 passport",
            "HTTP 401 Unauthorized",
            "账号异常，需要验证",
        ],
    )
    def test_auth_signals(self, text: str) -> None:
        assert classify(text) is FailureKind.AUTH

    @pytest.mark.parametrize(
        "text",
        [
            "操作过于频繁，请稍后再试",
            "HTTP 429 Too Many Requests",
            "触发安全验证",
            "请完成滑块验证",
            "疑似异常行为",
            "captcha required",
        ],
    )
    def test_risk_signals(self, text: str) -> None:
        assert classify(text) is FailureKind.RISK

    @pytest.mark.parametrize(
        "text",
        [
            "找不到联系人「小明」",
            "用户不存在",
            "对方已注销",
            "会话不存在",
            "HTTP 404",
            "已被拉黑",
        ],
    )
    def test_permanent_signals(self, text: str) -> None:
        assert classify(text) is FailureKind.PERMANENT

    @pytest.mark.parametrize(
        "text",
        [
            "操作超时",
            "Timeout 30000ms exceeded",
            "net::ERR_CONNECTION_RESET",
            "连接被重置",
            "元素不可见",
            "表情面板没有弹出",
            "输入框不可见",
        ],
    )
    def test_transient_signals(self, text: str) -> None:
        assert classify(text) is FailureKind.TRANSIENT

    @pytest.mark.parametrize(
        "text",
        [
            "图片文件不存在",
            "消息池为空",
        ],
    )
    def test_config_signals(self, text: str) -> None:
        assert classify(text) is FailureKind.CONFIG

    def test_unknown_for_other_text(self) -> None:
        assert classify("某种没见过的错误") is FailureKind.UNKNOWN

    def test_empty_is_unknown(self) -> None:
        assert classify("") is FailureKind.UNKNOWN

    def test_case_insensitive(self) -> None:
        assert classify("UNAUTHORIZED ACCESS") is FailureKind.AUTH

    def test_auth_wins_over_transient(self) -> None:
        """同时出现多个关键词时，更严重、更具体的要优先。

        「登录态超时」里既有「登录态」也有「超时」——
        必须判成 AUTH，否则会被无意义地重试。
        """
        assert classify("登录态超时，请重新登录") is FailureKind.AUTH

    def test_risk_wins_over_transient(self) -> None:
        assert classify("访问受限，请求超时") is FailureKind.RISK


class TestClassifyException:
    def test_timeout_exception(self) -> None:
        class TimeoutError_(Exception):
            pass

        assert classify_exception(TimeoutError_("boom")) is FailureKind.TRANSIENT

    def test_connection_exception(self) -> None:
        class ConnectionError_(Exception):
            pass

        assert classify_exception(ConnectionError_("reset")) is FailureKind.TRANSIENT

    def test_falls_back_to_message(self) -> None:
        assert classify_exception(ValueError("请先登录")) is FailureKind.AUTH

    def test_unknown_exception(self) -> None:
        assert classify_exception(ValueError("???")) is FailureKind.UNKNOWN


class TestRetryDecision:
    def test_permanent_not_retried(self) -> None:
        decision = decide_retry(FailureKind.PERMANENT, attempt=1)

        assert decision.should_retry is False
        assert "不会因重试而改善" in decision.reason

    def test_auth_aborts_task(self) -> None:
        """认证失效必须中止整个任务 —— 继续给其他收件人发毫无意义。"""
        decision = decide_retry(FailureKind.AUTH, attempt=1)

        assert decision.should_retry is False
        assert decision.abort_task is True
        assert FailureKind.AUTH in ABORT_TASK

    def test_config_not_retried(self) -> None:
        decision = decide_retry(FailureKind.CONFIG, attempt=1)

        assert decision.should_retry is False
        assert decision.abort_task is False

    def test_transient_retried_with_backoff(self) -> None:
        decision = decide_retry(FailureKind.TRANSIENT, attempt=1, backoff_seconds=30.0)

        assert decision.should_retry is True
        # 30s ± 20%
        assert 24.0 <= decision.delay_seconds <= 36.0

    def test_backoff_grows(self) -> None:
        first = decide_retry(FailureKind.TRANSIENT, attempt=1, backoff_seconds=30.0)
        second = decide_retry(FailureKind.TRANSIENT, attempt=2, backoff_seconds=30.0)

        assert second.delay_seconds > first.delay_seconds
        # 大约翻倍
        assert 48.0 <= second.delay_seconds <= 72.0

    def test_stops_at_max_retries(self) -> None:
        decision = decide_retry(FailureKind.TRANSIENT, attempt=3, max_retries=3)

        assert decision.should_retry is False
        assert "最大重试次数" in decision.reason

    def test_zero_retries_means_no_retry(self) -> None:
        decision = decide_retry(FailureKind.TRANSIENT, attempt=1, max_retries=0)

        assert decision.should_retry is False

    def test_risk_gets_one_attempt_with_cooldown(self) -> None:
        """风控只给一次机会，且必须等足够久。

        ⚠️ 回归：``attempt`` 的语义是「已尝试次数」（从 1 开始，生产里
        runner 就是这么传的）。以前这条用例传 ``attempt=0`` 才通过，
        而生产传的是 ≥1 → 冷却分支不可达、配置失效。
        """
        decision = decide_retry(FailureKind.RISK, attempt=1, risk_cooldown_seconds=300.0)

        assert decision.should_retry is True
        assert decision.delay_seconds == 300.0
        assert "冷却" in decision.reason

    def test_risk_not_retried_again(self) -> None:
        """冷却重试过一次之后，不能再试，而且要中止整个任务。"""
        decision = decide_retry(FailureKind.RISK, attempt=2)

        assert decision.should_retry is False
        assert "加重风险" in decision.reason
        assert decision.abort_task is True

    def test_unknown_is_retried_conservatively(self) -> None:
        """未知错误保守处理 —— 给一次重试，但也要有上限。"""
        decision = decide_retry(FailureKind.UNKNOWN, attempt=1)

        assert decision.should_retry is True

    def test_non_retryable_set_is_correct(self) -> None:
        assert FailureKind.PERMANENT in NON_RETRYABLE
        assert FailureKind.AUTH in NON_RETRYABLE
        assert FailureKind.CONFIG in NON_RETRYABLE
        assert FailureKind.TRANSIENT not in NON_RETRYABLE

    def test_delay_never_negative(self) -> None:
        for kind in FailureKind:
            for attempt in range(0, 5):
                decision = decide_retry(kind, attempt, backoff_seconds=0.01)
                assert decision.delay_seconds >= 0.0


class TestRetryBudget:
    def test_consume_decrements(self) -> None:
        budget = RetryBudget(remaining=3)

        assert budget.consume() is True
        assert budget.remaining == 2
        assert budget.used == 1

    def test_exhausts(self) -> None:
        budget = RetryBudget(remaining=2)

        assert budget.consume() is True
        assert budget.consume() is True
        assert budget.consume() is False
        assert budget.exhausted is True
        assert budget.used == 2

    def test_zero_budget_never_consumes(self) -> None:
        budget = RetryBudget(remaining=0)

        assert budget.consume() is False
        assert budget.used == 0

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValueError):
            RetryBudget(remaining=-1)

    def test_shared_across_contacts(self) -> None:
        """这是它存在的理由：3 个收件人共用一个预算，
        而不是每个都独立重试 3 次（那总共会是 9 次浏览器操作）。"""
        budget = RetryBudget(remaining=2)

        assert budget.consume() is True  # 收件人 A 第一次重试
        assert budget.consume() is True  # 收件人 B 第一次重试
        assert budget.consume() is False  # 收件人 C 没预算了


class TestSummarizeFailures:
    def test_all_success(self) -> None:
        outcomes = [
            TargetOutcome(name="A", status=RunStatus.SUCCESS, sent=1),
            TargetOutcome(name="B", status=RunStatus.SUCCESS, sent=1),
        ]

        summary = summarize_failures(outcomes)
        assert summary["failure_count"] == 0
        assert summary["failed_contacts"] == []

    def test_counts_by_kind(self) -> None:
        outcomes = [
            TargetOutcome(name="A", status=RunStatus.FAILED, failure_kind=FailureKind.TRANSIENT),
            TargetOutcome(name="B", status=RunStatus.FAILED, failure_kind=FailureKind.TRANSIENT),
            TargetOutcome(name="C", status=RunStatus.FAILED, failure_kind=FailureKind.AUTH),
            TargetOutcome(name="D", status=RunStatus.SUCCESS, sent=1),
        ]

        summary = summarize_failures(outcomes)
        assert summary["failure_count"] == 3
        assert summary["failed_contacts"] == ["A", "B", "C"]
        assert summary["by_kind"]["transient"] == 2
        assert summary["by_kind"]["auth"] == 1

    def test_uncertain_counts_as_failure(self) -> None:
        """UNCERTAIN 不是成功 —— 汇总时必须算进失败侧，
        否则「3 个里 2 个不确定」会被显示成一片祥和。"""
        outcomes = [
            TargetOutcome(name="A", status=RunStatus.UNCERTAIN),
        ]

        summary = summarize_failures(outcomes)
        assert summary["failure_count"] == 1
