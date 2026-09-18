"""错误分类与重试策略。

把一个失败正确归类，直接决定接下来该做什么：

==============  ========================================  ==========================
类别             典型场景                                   处理
==============  ========================================  ==========================
TRANSIENT       网络抖动、页面加载慢、元素暂时不可见          退避后重试，最多 N 次
PERMANENT       好友名字对不上、会话不存在                   不重试，直接告警
AUTH            登录态失效、被踢下线                         **立即中止整个任务**
RISK            疑似风控、频率限制                           等冷却时间，只试一次
CONFIG          配置有误（图片文件不存在、表情名写错）        不重试，提示改配置
UNKNOWN         无法归类的异常                              保守当作可重试一次
==============  ========================================  ==========================

**AUTH 和 RISK 必须中止整个任务**，而不是跳过当前收件人继续下一个。
理由：如果登录态已经失效，继续尝试剩下的收件人只会产生一串毫无意义的失败，
还会因为反复访问而加重风控。停下来、报警、等人工介入，是唯一正确的选择。
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any

from ..models import FailureKind

LOGGER = logging.getLogger(__name__)

# 从错误信息里识别类别的关键词。顺序有意义 —— 越具体的放前面。
_PATTERNS: tuple[tuple[FailureKind, tuple[str, ...]], ...] = (
    # --- 认证失效：必须立即停止 ---
    (
        FailureKind.AUTH,
        (
            "登录态",
            "未登录",
            "请先登录",
            "扫码登录",
            "登录失效",
            "登录过期",
            "登录页",
            "passport",  # 抖音/字节系的登录域，会出现在重定向 URL 和报错里
            "sessionid",
            "unauthorized",
            "401",
            "账号异常",
            "需要验证",
        ),
    ),
    # --- 风控：降速 + 可能中止 ---
    (
        FailureKind.RISK,
        (
            "风控",
            "频率",
            "操作过于频繁",
            "too many requests",
            "429",
            "访问受限",
            "安全验证",
            "滑块",
            "验证码",
            "captcha",
            "异常行为",
        ),
    ),
    # --- 永久失败：重试无用 ---
    (
        FailureKind.PERMANENT,
        (
            "找不到联系人",
            "找不到会话",
            "用户不存在",
            "对方已注销",
            "会话不存在",
            "not found",
            "404",
            "已被拉黑",
        ),
    ),
    # --- 配置错误 ---
    (
        FailureKind.CONFIG,
        (
            "文件不存在",
            "配置",
            "未指定",
            "消息池为空",
        ),
    ),
    # --- 临时性：可以重试 ---
    (
        FailureKind.TRANSIENT,
        (
            "超时",
            "timeout",
            "timed out",
            "网络",
            "net::",
            "econnreset",
            "连接",
            "元素不可见",
            "still loading",
            "没有弹出",
            "未能确认",
            "输入框",
        ),
    ),
)

# 这些类别重试没有意义
NON_RETRYABLE = frozenset(
    {
        FailureKind.PERMANENT,
        FailureKind.AUTH,
        FailureKind.CONFIG,
    }
)

# 这些类别必须中止整个任务
ABORT_TASK = frozenset({FailureKind.AUTH})


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """重试决策的结果。"""

    should_retry: bool
    delay_seconds: float
    reason: str
    abort_task: bool = False


def classify(message: str, *, default: FailureKind = FailureKind.UNKNOWN) -> FailureKind:
    """从错误文本推断类别。

    这是一个**基于关键词的启发式**，不是精确判断。之所以这样做而不是让
    每个调用点自己传类别：实际出错时抛上来的往往只是一段字符串
    （Playwright 的 TimeoutError、requests 的异常文本），调用点并不知道
    该归到哪一类。集中在这里至少保证分类逻辑一致。

    匹配不上的归为 UNKNOWN，由重试策略保守处理。
    """
    text = (message or "").lower()
    if not text:
        return default

    for kind, keywords in _PATTERNS:
        for keyword in keywords:
            if keyword.lower() in text:
                LOGGER.debug("错误归类为 %s（命中关键词 %r）", kind.value, keyword)
                return kind

    return default


def classify_exception(exc: BaseException) -> FailureKind:
    """从异常对象推断类别。"""
    # Playwright 的超时是临时性的
    name = type(exc).__name__
    if "Timeout" in name:
        return FailureKind.TRANSIENT
    if "Connection" in name or "Network" in name:
        return FailureKind.TRANSIENT

    return classify(str(exc))


def decide_retry(
    kind: FailureKind,
    attempt: int,
    *,
    max_retries: int = 3,
    backoff_seconds: float = 30.0,
    risk_cooldown_seconds: float = 300.0,
) -> RetryDecision:
    """决定要不要重试、等多久。

    ``attempt`` 是**已经尝试过的次数**（从 1 开始）。第 1 次失败后
    ``attempt=1``，此时如果 ``max_retries=3`` 还有 2 次机会。

    退避策略：指数递增 + 随机抖动。抖动是为了避免多个收件人的重试
    在同一秒发生（那本身的特征就很机器）。
    """
    if kind in NON_RETRYABLE:
        return RetryDecision(
            should_retry=False,
            delay_seconds=0.0,
            reason=f"{kind.value} 类型不会因重试而改善",
            abort_task=kind in ABORT_TASK,
        )

    if kind is FailureKind.RISK:
        # 风控：等冷却时间后只再试**一次**；再失败就停手，并中止整个任务。
        #
        # ⚠️ ``attempt`` 的语义是「已经尝试过的次数」（从 1 开始），
        # 所以这里必须写 ``> 1``。以前写的是 ``>= 1`` —— 恒为真，
        # 于是「等冷却后重试一次」这段代码**永远不可达**，
        # HUOHUA_RISK_COOLDOWN_SEC 这个配置也形同虚设（实测踩过）。
        if attempt > 1:
            return RetryDecision(
                should_retry=False,
                delay_seconds=0.0,
                reason="已命中风控且冷却重试过一次，停止本次运行（继续尝试只会加重风险）",
                abort_task=True,
            )
        return RetryDecision(
            should_retry=True,
            delay_seconds=risk_cooldown_seconds,
            reason=f"疑似风控，等待 {risk_cooldown_seconds:.0f}s 冷却后再试一次",
        )

    if attempt >= max_retries:
        return RetryDecision(
            should_retry=False,
            delay_seconds=0.0,
            reason=f"已达最大重试次数（{max_retries}）",
        )

    # 指数退避：30s → 60s → 120s …
    base = backoff_seconds * (2 ** (attempt - 1))
    # ±20% 抖动
    jitter = base * random.uniform(-0.2, 0.2)
    delay = max(0.0, base + jitter)

    return RetryDecision(
        should_retry=True,
        delay_seconds=delay,
        reason=f"第 {attempt + 1} 次尝试将在 {delay:.0f}s 后进行",
    )


@dataclass
class RetryBudget:
    """跨收件人共享的重试预算。

    为什么需要这个：如果给每个收件人独立的 ``max_retries``，
    3 个收件人 × 3 次重试 = 12 次浏览器操作，一旦是风控相关的问题，
    这个量级足以把事情搞砸。

    所以设一个全局上限，用完了就停止重试、直接报告。
    """

    remaining: int
    used: int = 0

    def __post_init__(self) -> None:
        if self.remaining < 0:
            raise ValueError("重试预算不能为负")

    def consume(self) -> bool:
        """消耗一次预算。返回 False 表示预算已用完。"""
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        self.used += 1
        return True

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0


def sleep_with_logging(seconds: float, *, reason: str = "") -> None:
    """可读的等待。

    分片 sleep 而不是一次睡到底：这样 Ctrl-C 能及时响应，
    而不是「按了没反应，等 120 秒才退出」。
    """
    if seconds <= 0:
        return

    if reason:
        LOGGER.info("等待 %.0fs：%s", seconds, reason)

    remaining = seconds
    while remaining > 0:
        chunk = min(remaining, 1.0)
        time.sleep(chunk)
        remaining -= chunk


def summarize_failures(outcomes: list[Any]) -> dict[str, Any]:
    """汇总一批结果里的失败类别分布。工作台的运行详情页用它。"""
    from ..models import RunStatus

    by_kind: dict[str, int] = {}
    failed: list[str] = []

    for outcome in outcomes:
        status = getattr(outcome, "status", None)
        if status is not None and status != RunStatus.SUCCESS:
            name = getattr(outcome, "contact_name", None) or getattr(outcome, "name", "?")
            failed.append(name)
            kind = getattr(outcome, "failure_kind", None)
            key = kind.value if isinstance(kind, FailureKind) else "unknown"
            by_kind[key] = by_kind.get(key, 0) + 1

    return {
        "failed_contacts": failed,
        "failure_count": len(failed),
        "by_kind": by_kind,
    }


__all__ = [
    "ABORT_TASK",
    "NON_RETRYABLE",
    "RetryBudget",
    "RetryDecision",
    "classify",
    "classify_exception",
    "decide_retry",
    "sleep_with_logging",
    "summarize_failures",
]
