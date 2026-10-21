"""单次执行编排。

这个模块回答一个问题：**「今天的这一次发送，从开始到结束都发生了什么」。**

流程：

1. 拿全局运行锁 —— 防止定时任务和手动触发同时跑
2. 读取配置，冻结成一个 :class:`TaskPlan`（中途改网页不影响正在跑的这一轮）
3. 启动浏览器、加载登录态
4. 探测会话页 —— 登录态失效就立刻中止 + 发 RISK 告警
5. 逐个收件人发送，间隔加随机抖动
6. 每个收件人失败后按错误类别决定是否重试，共享一个重试预算
7. 汇总成 :class:`RunReport`，落盘、更新连续失败计数、按级别告警

**关于错峰**：多个收件人之间会等待 ``SendInterval`` 范围内的随机时长。
这既有「避免同一秒批量发送」的实际考虑，也让行为看起来不那么整齐 ——
不整齐本身不是目的，但整齐确实是异常特征。
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import runstate
from ..config import Settings, load_settings
from ..models import (
    AlertLevel,
    Contact,
    FailureKind,
    Message,
    RunReport,
    RunStatus,
    TargetOutcome,
    TaskPlan,
)
from ..notify import Alert, Dispatcher, level_from_state
from ..store import LockBusyError, Repository, new_run_id, now_str
from ..store.repo import repository_for

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RunContext:
    """一次执行的上下文，供报告和告警文案使用。"""

    run_id: str
    started_at: str
    dry_run: bool
    target_count: int


class RunAbortedError(RuntimeError):
    """执行被主动中止（登录态失效、风控、无法获取锁）。"""


def resolve_prevent_duplicates(kind: str, explicit: bool | None) -> bool:
    """决定这一轮要不要执行「当日防重复」。

    规则：**只有定时任务防重复；用户明确点下的发送一律真发。**

    ``explicit`` 给定时以它为准（路由层可以显式覆盖）；留空时按 ``kind`` 推断。

    ⚠️ 这条以前是错的 —— 判定依据写成了「有没有传 messages_override」。
    于是工作台首页那个「立即发送一次」按钮（不传 message）也被当成定时任务，
    点下去只得到一条「今天已经发送过（防重复）」的跳过记录：用户以为发了，
    实际一条都没出去。现在按**触发来源**判断，语义不会再被间接条件带偏。
    """
    if explicit is not None:
        return explicit
    return kind == "scheduled"


# =============================================================================
# 主入口
# =============================================================================


def run_once(
    settings: Settings | None = None,
    *,
    repository: Repository | None = None,
    dry_run: bool | None = None,
    contacts_override: tuple[Contact, ...] | None = None,
    messages_override: tuple[Message, ...] | None = None,
    kind: str = "manual",
    prevent_duplicates: bool | None = None,
) -> RunReport | None:
    """执行一次发送任务。

    ``contacts_override`` 让工作台能「只给这几个联系人发一次」，
    而不改变保存的配置 —— 这是「每次执行可以自由选人」的实现方式。

    ``messages_override`` 同理：临时用这条消息，不改文案池。

    ``prevent_duplicates`` 决定要不要执行「当日防重复」。留空时按 ``kind`` 推断：
    **只有定时任务（``kind="scheduled"``）才防重复；用户明确点下的发送一律真发。**

    ⚠️ 这条以前是错的：判定依据写成「有没有传 messages_override」，于是工作台
    首页那个「立即发送一次」按钮（不传 message）也被当成定时任务，
    点下去只会得到一条「今天已经发送过（防重复）」的跳过记录 —— 用户以为发了，
    实际一条都没出去（实测事故）。现在改成按**触发来源**决定，
    语义清晰：自动的防重复，手动的永远真发。
    """
    prevent_duplicates = resolve_prevent_duplicates(kind, prevent_duplicates)

    settings = settings or load_settings()
    repo = repository or repository_for(settings)
    repo.ensure_layout()

    is_dry_run = settings.send.dry_run if dry_run is None else dry_run

    # --- 1. 全局锁 ---
    guard = _acquire_lock(repo)
    with guard as lock_held:
        if not lock_held:
            # 锁被别人占着：不是一个「失败」，而是「这次跳过」。
            #
            # ⚠️ 但**定时任务**被挡下必须让人知道（2026-10-21 事故）：
            #    残留锁会让每天的定时发送静默跳过、只留一行日志，
            #    用户以为在正常发，实际一整天没人收到消息。
            LOGGER.warning("已有任务在运行，本次触发跳过")
            if kind == "scheduled":
                _notify_skipped_by_lock(settings)
            return None

        # --- 2. 冻结计划 ---
        plan = _build_plan(
            repo,
            settings,
            contacts_override=contacts_override,
            messages_override=messages_override,
            prevent_duplicates=prevent_duplicates,
        )
        if plan is None:
            LOGGER.info("没有启用的收件人或消息，跳过本次运行")
            return None

        context = RunContext(
            run_id=new_run_id(),
            started_at=now_str(),
            dry_run=is_dry_run,
            target_count=len(plan.targets),
        )

        LOGGER.info(
            "开始运行 %s：%d 个收件人，消息类型 %s%s",
            context.run_id,
            context.target_count,
            {m.kind for m in plan.messages},
            "（演练模式）" if is_dry_run else "",
        )

        # --- 3-7. 执行 ---
        return _execute(settings, repo, plan, context, is_dry_run, kind=kind, lock=guard)


# =============================================================================
# 执行主体
# =============================================================================


def _detect_suspected_renames(
    *, configured: set[str], missing: list[str], seen: set[str]
) -> tuple[str, str] | None:
    """用「找不到的人 + 列表里陌生的名字」推断疑似改名。

    为什么只能"推断"：配置里只存名字（``conversation_id`` 是 null），好友改名后
    我们只知道「原来那个名字找不到了」，**拿不到新昵称**。但会话列表是能读全的 ——
    列表里出现一个配置里没有的名字，就很可能是它。

    - 1 个缺失 + 1 个陌生名 → 直接给出配对，可信度高；
    - 其它情况 → 把两组都列出来，让人自己确认（不硬猜）。

    返回 ``(去重键, 给人看的正文)``；没有值得提醒的线索时返回 ``None``。
    """
    # 惰性导入：避免 runner ↔ engine 的顶层循环依赖
    from ..engine.navigator import normalize_name

    known = {normalize_name(c) for c in configured}
    unknown = sorted(n for n in seen if normalize_name(n) not in known)
    if not missing or not unknown:
        return None

    if len(missing) == 1 and len(unknown) == 1:
        old, new = missing[0], unknown[0]
        key = f"{normalize_name(old)}->{normalize_name(new)}"
        body = (
            f"**疑似改名：{old} → {new}**\n\n"
            f"配置里的「{old}」在会话列表里找不到了；\n"
            f"而列表里多了一个配置里没有的名字「{new}」。\n\n"
            "**建议动作**：如果确实是改名，去工作台把这个收件人的名字改成新昵称；"
            "不改的话这个人每天都会发送失败。\n\n"
            "（这条提醒对同一次改名只发一次，改好后不会再打扰。）"
        )
        return key, body

    key = "multi:" + "|".join(normalize_name(x) for x in [*missing, *unknown][:20])
    body = (
        "**疑似有好友改名**（有好几个对不上，无法自动配对，需要你确认）\n\n"
        "配置里找不到：" + "、".join(missing) + "\n"
        "列表里的陌生名字：" + "、".join(unknown[:10]) + ("…" if len(unknown) > 10 else "") + "\n\n"
        "**建议动作**：去工作台「收件人」页核对一下昵称，改好后就不会再失败。"
    )
    return key, body


def _alert_suspected_renames(
    settings: Settings,
    repo: Repository,
    engine: Any,
    *,
    plan: TaskPlan,
    missing: list[str],
) -> None:
    """读全部会话 → 推断改名 → 去重后推送。

    **任何异常都吞掉**：这是附加提醒，绝不能影响发送结果。
    """
    from ..engine import navigator
    from ..store.atomic import atomic_write_json, read_json

    try:
        details = engine.session.call(
            lambda ctx: navigator.collect_conversation_details(ctx.page), timeout=240
        )
        seen = {str(d.get("name") or "").strip() for d in details}
        seen.discard("")
        result = _detect_suspected_renames(
            configured={c.name for c in plan.targets}, missing=missing, seen=seen
        )
        if result is None:
            LOGGER.info("疑似改名检测：没有值得提醒的线索（缺失 %s，陌生名 %s）", missing, sorted(seen)[:5])
            return
        key, body = result

        # 去重：同一次改名只提醒一次，避免每天重复打扰
        path = repo.config_dir / "rename_alerts.json"
        payload = read_json(path, default={}) or {}
        alerted = payload.get("alerted") or {}
        if key in alerted:
            LOGGER.info("疑似改名检测：%s 已经提醒过，跳过", key)
            return
        alerted[key] = now_str()
        payload["alerted"] = alerted
        atomic_write_json(path, payload)

        Dispatcher.from_settings(settings.notify).dispatch(
            Alert(level=AlertLevel.NOTICE, title="🔄 疑似有好友改名", body=body)
        )
        LOGGER.warning("疑似改名，已推送提醒：%s", key)
    except Exception as exc:  # noqa: BLE001 —— 附加提醒，绝不能影响发送结果
        LOGGER.warning("疑似改名检测失败（不影响发送）：%s", exc)


def _refresh_state_mtime(state_path: Any) -> None:
    """登录态被验证可用后，刷新它的文件时间戳。

    **绝不能因为这里出错影响任务结果** —— 它只是把「保守寿命」的起点往后挪，
    失败了顶多是下次早点提醒你重登。所以一切异常都吞掉，只记日志。
    """
    path = Path(state_path)
    if not path.is_file():
        # 不要凭空造一个空文件出来 —— 那会让后续的登录态检查看到一份「存在但无效」的状态，
        # 反而把「没登录过」这件事说得更含糊。
        return
    try:
        path.touch()
        LOGGER.info("本次有发送成功，已刷新登录态文件时间戳（保守寿命从今天重新计）")
    except OSError as exc:
        LOGGER.warning("刷新登录态文件时间戳失败（不影响本次发送结果）：%s", exc)


def _execute(
    settings: Settings,
    repo: Repository,
    plan: TaskPlan,
    context: RunContext,
    dry_run: bool,
    *,
    kind: str = "manual",
    lock: Any = None,
) -> RunReport:
    from ..common import errors as err_mod
    from ..engine import Engine
    from ..engine.auth import check_cookie_freshness
    from ..engine.composer import pick_message

    outcomes: list[TargetOutcome] = []
    abort_reason: str | None = None
    auth_expired = False
    risk_detected = False

    # 重试预算在本次运行内共享。3 个收件人共用 5 次，而不是各 3 次。
    retry_budget = err_mod.RetryBudget(remaining=max(1, settings.send.max_retries + 2))

    rng = random.Random()

    # 进度单例：把「正在给谁发 / 第几个」暴露给工作台首页。
    # 所有写都内部吞异常，即使这里出问题也绝不能打断真正的发送。
    runstate.begin(
        kind=kind,
        dry_run=dry_run,
        run_id=context.run_id,
        targets=[c.name for c in plan.targets],
    )

    account_id = _default_account_id(repo)
    state_path = repo.account_state_path(account_id)

    # --- 发送前的本地检查（零成本）---
    freshness = check_cookie_freshness(state_path)
    LOGGER.info("登录态：%s", freshness.detail)

    if freshness.expired and not dry_run:
        auth_expired = True
        abort_reason = freshness.detail

    engine: Engine | None = None

    try:
        if abort_reason is None:
            engine = Engine(settings.browser, settings.send)
            engine.start()

            # 演练模式下允许没有登录态 —— 这时 load_state 会因为没有 cookie
            # 而抛错，但演练的本意正是「还没绑定账号时也能验证整条链路」。
            # 页面级探测在演练模式下也会被跳过（见下），所以不会误判。
            try:
                engine.load_state(state_path)
            except Exception as exc:
                if not dry_run:
                    raise
                LOGGER.info("演练模式：未加载登录态（%s）", exc)

            # --- 页面级登录态探测 ---
            #
            # 演练模式**也要做这一步**。
            #
            # 否则页面根本不会导航到 /chat，仍然停在 about:blank，
            # 后续所有选择器都会因为「页面是空的」而失败 ——
            # 那样的演练验证不了任何真实链路，是假验证（实测踩过）。
            #
            # 但演练模式**容忍失败**：演练的本意正是「账号还没绑定时
            # 也能走一遍完整流程」，所以探测不通过不中止，只记日志。
            check = engine.check_login()
            LOGGER.info("会话页探测：%s", check.detail)
            if not check.ok and not dry_run:
                if check.reason == "AUTH_EXPIRED":
                    auth_expired = True
                abort_reason = check.detail
                runstate.set_stage(f"中止：{abort_reason}")

        if abort_reason is None:
            for index, contact in enumerate(plan.targets):
                # 错峰：第一个不用等，后续的等一段随机时间
                if index > 0:
                    gap = rng.uniform(plan.interval.minimum, plan.interval.maximum)
                    runstate.set_stage(f"错峰等待中，下一位：{contact.name}")
                    err_mod.sleep_with_logging(gap, reason=f"错峰（{contact.name}）")

                runstate.start_target(contact.name, index + 1)
                # 错峰等待后立刻刷心跳：错峰最长 90s，别让锁被判成残留
                if lock is not None:
                    lock.touch()
                outcome, aborted = _send_one(
                    engine=engine,  # type: ignore[arg-type]
                    contact=contact,
                    plan=plan,
                    settings=settings,
                    repo=repo,
                    dry_run=dry_run,
                    retry_budget=retry_budget,
                    pick_message=pick_message,
                    rng=rng,
                )
                outcomes.append(outcome)
                runstate.mark_outcome(outcome.status.value)
                if lock is not None:
                    lock.touch()

                if outcome.failure_kind is FailureKind.AUTH:
                    auth_expired = True
                # 浏览器卡死：后续每个人都会立即失败，直接中止本轮（省时间、早告警）
                elif "浏览器已判定卡死" in (outcome.detail or ""):
                    aborted = True
                if outcome.failure_kind is FailureKind.RISK:
                    risk_detected = True

                if aborted:
                    abort_reason = outcome.detail
                    runstate.set_stage(f"任务中止：{abort_reason}")
                    LOGGER.warning("任务中止：%s", abort_reason)
                    break

    except Exception as exc:
        LOGGER.exception("运行过程出现未预期异常")
        abort_reason = f"{type(exc).__name__}: {exc}"
    finally:
        if engine is not None:
            engine.stop()
        # 无论成功/失败/异常，都结束进度记录
        runstate.finish()

    # --- 一个人都没轮到就中止了 ---
    # 这种情况（登录态失效、浏览器起不来、打开会话页报错）必须体现到报告里。
    # 否则报告会是「成功 0/0，失败 0」，看起来像「什么都没发生」，
    # 而实际上你今天的火花没续上 —— 这是最危险的静默失败。
    if not outcomes and abort_reason:
        outcomes = [
            TargetOutcome(
                name=contact.name,
                status=RunStatus.FAILED,
                failure_kind=(FailureKind.AUTH if auth_expired else FailureKind.TRANSIENT),
                detail=f"任务在开始前中止：{abort_reason}",
            )
            for contact in plan.targets
        ]
        LOGGER.warning("任务在给任何人发送前就中止了：%s", abort_reason)

    # --- 疑似改名检测（附加提醒，任何失败都不影响发送结果）---
    #
    # 配置里只存名字，好友改名后只会「找不到」、拿不到新昵称 ——
    # 于是用「找不到的人 + 会话列表里陌生的名字」来推断，1 对 1 时直接给出配对。
    # 只在真的出现「找不到联系人」时才做（否则没必要多滚一遍列表）。
    not_found = [
        o.name for o in outcomes if o.status is RunStatus.FAILED and "找不到联系人" in (o.detail or "")
    ]
    if not_found and not dry_run and engine is not None:
        _alert_suspected_renames(settings, repo, engine, plan=plan, missing=not_found)

    # --- 汇总 ---
    return _finalize(
        settings=settings,
        repo=repo,
        plan=plan,
        context=context,
        outcomes=outcomes,
        abort_reason=abort_reason,
        auth_expired=auth_expired,
        risk_detected=risk_detected,
        dry_run=dry_run,
        kind=kind,
    )


def _send_one(
    *,
    engine: Any,
    contact: Contact,
    plan: TaskPlan,
    settings: Settings,
    repo: Repository,
    dry_run: bool,
    retry_budget: Any,
    pick_message: Any,
    rng: random.Random,
) -> tuple[TargetOutcome, bool]:
    """给一个联系人发送，带重试。返回 ``(结果, 是否应中止整个任务)``。"""
    from ..common import errors as err_mod

    # 防重复：今天已经发过就跳过
    today = time.strftime("%Y-%m-%d")
    if plan.prevent_duplicates and contact.sent_today(today) and not dry_run:
        LOGGER.info("跳过 %s：今天已发送过", contact.name)
        return (
            TargetOutcome(
                name=contact.name,
                status=RunStatus.SKIPPED,
                detail="今天已经发送过（防重复）",
            ),
            False,
        )

    attempt = 0

    while True:
        attempt += 1

        message = pick_message(plan.messages, rng=rng)
        LOGGER.info("发送给 %s（第 %d 次尝试）：%s", contact.name, attempt, message.kind)

        # allow_search=True：会话列表滚到底都找不到时，再用搜索兜一层。
        # 「宁可多一次请求，也不能少发一个人」—— 少发一个人 = 火花断。
        result = engine.send_to_contact(contact, message, dry_run=dry_run, allow_search=True)

        if result.status is RunStatus.SUCCESS:
            # 成功 → 记下今天已发（防重复依据）
            if not dry_run:
                try:
                    repo.mark_sent_today((contact.name,))
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning("记录发送状态失败：%s", exc)
            return result.to_target_outcome(), False

        # ⚠️ 「结果不确定」绝不能重试。
        #
        # confirmer 把这种情形标成 ``failure_kind=TRANSIENT``（它确实是临时性的），
        # 但状态是 ``UNCERTAIN`` —— 含义是「**消息可能已经发出去了**，只是没能确认」。
        # 光看 failure_kind 会走进「重试」分支，于是**重复发送**。
        # 这是这个工具最不能出的错（对方会收到两条），所以在这里硬拦一道。
        if result.status is RunStatus.UNCERTAIN:
            LOGGER.warning("结果不确定（消息可能已发出），不重试以免重复发送：%s", result.detail)
            return result.to_target_outcome(), False

        kind = result.failure_kind or FailureKind.UNKNOWN
        decision = err_mod.decide_retry(
            kind,
            attempt,
            max_retries=settings.send.max_retries,
            backoff_seconds=settings.send.retry_backoff_sec,
            risk_cooldown_seconds=settings.send.risk_cooldown_sec,
        )

        if decision.abort_task:
            LOGGER.error("命中需中止的失败类型（%s），停止本次运行", kind.value)
            return result.to_target_outcome(), True

        if not decision.should_retry:
            LOGGER.info("不重试：%s", decision.reason)
            return result.to_target_outcome(), False

        if not retry_budget.consume():
            LOGGER.warning("重试预算已用完，放弃重试")
            return result.to_target_outcome(), False

        err_mod.sleep_with_logging(decision.delay_seconds, reason=decision.reason)

        # 重试前再确认一次登录态 —— 如果是登录失效，不用等三次重试才发现
        if kind is FailureKind.TRANSIENT:
            ready = engine.check_login()
            if ready.reason == "AUTH_EXPIRED":
                LOGGER.error("重试前检测到登录态失效，停止重试")
                return result.to_target_outcome(), True


def _finalize(
    *,
    settings: Settings,
    repo: Repository,
    plan: TaskPlan,
    context: RunContext,
    outcomes: list[TargetOutcome],
    abort_reason: str | None,
    auth_expired: bool,
    risk_detected: bool,
    dry_run: bool,
    kind: str = "manual",
) -> RunReport:
    """生成报告、落盘、更新计数、发告警。"""
    outcomes_tuple = tuple(outcomes)

    # 记录连续失败 / 成功
    if dry_run:
        streak = repo.load_streak_state()
        consecutive = int(streak.get("consecutive_failures") or 0)
    elif not outcomes_tuple or all(o.status is RunStatus.SKIPPED for o in outcomes_tuple):
        # 没有任何**真实的发送尝试**（计划为空，或全部被防重复跳过）——
        # 这不该计入连续失败：什么都没做，谈不上失败。
        # 计入会让「连续失败」这个指标失真，进而误报 CRITICAL。
        #
        # ⚠️ 回归：以前这里只挡了「一条结果都没有」的情况。而「只有 SKIPPED」
        # 同样是「没做任何事」，却会掉进下面的 else 被当成失败 → record_failure()
        # 把连续成功清零；连续几天就会误报 WARNING/CRITICAL（实测踩过）。
        streak = repo.load_streak_state()
        consecutive = int(streak.get("consecutive_failures") or 0)
        LOGGER.info("本次没有任何真实发送尝试（全部跳过），不计入连续失败")
    else:
        succeeded = any(o.status is RunStatus.SUCCESS for o in outcomes_tuple)
        streak = repo.record_success() if succeeded else repo.record_failure()
        consecutive = int(streak.get("consecutive_failures") or 0)

    report = RunReport(
        run_id=context.run_id,
        account_id=_default_account_id(repo),
        started_at=context.started_at,
        finished_at=now_str(),
        dry_run=dry_run,
        outcomes=outcomes_tuple,
        consecutive_failures=consecutive,
    )

    # 落盘（演练模式不写，免得污染历史）
    if not dry_run:
        try:
            repo.save_report(report)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("保存运行报告失败：%s", exc)

        # 有人发送成功 ⇒ **这份登录态此刻是活的** ⇒ 刷新状态文件时间戳。
        #
        # 为什么必须做：`check_cookie_freshness` 是纯本地判断，靠的是**文件年龄**，
        # 而文件只在登录时写一次 —— 于是明明一直好用，攒够「保守寿命」也会被判过期、
        # 催你重新扫码（假警报）。刷新之后，文件年龄的真实含义变成
        # 「距上次验证登录态可用过了多久」：正常使用永远是 0 天，
        # 只有连着几天没跑成/没成功才会涨上来提醒你。
        #
        # 只看**任一成功**就够：能发出去一条，就证明会话还有效。
        #
        # 路径在这里自己取一次（`_finalize` 拿不到 `_execute` 的局部变量），
        # 取不到就当没这回事 —— 绝不因为刷新时间戳失败而影响任务结果。
        if any(o.status is RunStatus.SUCCESS for o in outcomes_tuple):
            try:
                _refresh_state_mtime(repo.account_state_path(_default_account_id(repo)))
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("跳过刷新登录态时间戳：%s", exc)

    # --- 告警 ---
    #
    # 计数「这一轮」的失败，而不是只看跨天累计 —— 否则「早上成功过一次、
    # 之后 15 人全失败」会被判成 NORMAL，一条通知都不发（实测事故）。
    failed_now = sum(1 for o in outcomes_tuple if o.status is RunStatus.FAILED)
    uncertain_now = sum(1 for o in outcomes_tuple if o.status is RunStatus.UNCERTAIN)

    level = level_from_state(
        consecutive_failures=consecutive,
        auth_expired=auth_expired,
        risk_detected=risk_detected,
        failed_now=failed_now,
        uncertain_now=uncertain_now,
        warn_threshold=settings.notify.warn_threshold,
        critical_threshold=settings.notify.critical_threshold,
    )

    _dispatch_alert(
        settings=settings,
        report=report,
        level=level,
        abort_reason=abort_reason,
        auth_expired=auth_expired,
        risk_detected=risk_detected,
        failed_now=failed_now,
        uncertain_now=uncertain_now,
        # 定时任务（09:30 那次）无论成功失败都要推一条结果汇总 ——
        # 用户明确要求「任务执行完毕后发一次」，不能只在失败时才说话。
        force_summary=(kind == "scheduled"),
    )

    LOGGER.info("运行结束 %s：%s", report.run_id, report.summary())
    return report


def _dispatch_alert(
    *,
    settings: Settings,
    report: RunReport,
    level: AlertLevel,
    abort_reason: str | None,
    auth_expired: bool,
    risk_detected: bool,
    failed_now: int = 0,
    uncertain_now: int = 0,
    force_summary: bool = False,
) -> None:
    """按级别发告警。

    ``force_summary=True`` 用于定时任务：即使全部成功也要推一条结果汇总
    （用户明确要求「09:30 任务跑完发一次」）。
    这种情况下把级别抬到 NOTICE —— NORMAL 是「刻意静默」的语义，
    不能用来表达「主动汇报」。
    """
    dispatcher = Dispatcher.from_settings(settings.notify)

    if report.dry_run:
        return

    if level is AlertLevel.NORMAL and not force_summary:
        # 正常情况默认不推送，但记一条日志方便回溯
        LOGGER.info("告警级别 %s，未推送", level.value)
        return

    effective = AlertLevel.NOTICE if (force_summary and level is AlertLevel.NORMAL) else level

    title, body = _craft_alert_text(
        report=report,
        level=effective,
        abort_reason=abort_reason,
        auth_expired=auth_expired,
        risk_detected=risk_detected,
        failed_now=failed_now,
        uncertain_now=uncertain_now,
        force_summary=force_summary,
    )

    alert = Alert(
        level=effective,
        title=title,
        body=body,
        context={
            "run_id": report.run_id,
            "summary": report.summary(),
            "consecutive_failures": report.consecutive_failures,
            "failed_now": failed_now,
            "uncertain_now": uncertain_now,
        },
    )

    dispatch_report = dispatcher.dispatch(alert)
    LOGGER.info("告警分发结果：%s", dispatch_report.summary())


def _craft_alert_text(
    *,
    report: RunReport,
    level: AlertLevel,
    abort_reason: str | None,
    auth_expired: bool,
    risk_detected: bool,
    failed_now: int = 0,
    uncertain_now: int = 0,
    force_summary: bool = False,
) -> tuple[str, str]:
    """组织告警文案。

    原则：**先说要做什么，再说发生了什么。** 你在手机上看到推送时
    最想知道的是「我需要动手吗」，而不是运行细节。
    """
    if risk_detected:
        title = "🚨 疑似风控，请暂停使用"
        body = (
            "抖音返回了疑似风控的信号。\n\n"
            "**建议动作**：今天就别再试了，明天换个时间段再发。\n"
            "如果连续两天都报风控，建议停用一周观察账号状态。\n\n"
            "硬扛只会让情况变糟 —— 账号被限制的代价远大于断几天火花。"
        )
        return title, body

    if auth_expired:
        title = "🔑 登录态已过期，需要重新扫码"
        body = (
            f"{abort_reason or '登录态已失效'}。\n\n"
            "**建议动作**：打开工作台「账号」页点「重新扫码」，用手机抖音扫一下，"
            "整个过程不超过 30 秒。**扫完不用重启容器** —— 每次任务开始时会重新读取登录态。\n\n"
            "这次的失败原因就是登录态，与「发送结果无法确认」无关："
            "后者是「消息可能已发出、只是没确认到」，**不会**用这个标题、也不会让你白扫一次码。"
        )
        return title, body

    if level is AlertLevel.CRITICAL:
        title = f"❗ 已连续 {report.consecutive_failures} 天发送失败"
        body = (
            f"{report.summary()}\n\n"
            "**建议动作**：打开工作台看看失败原因。常见的有："
            "登录态其实已失效、好友改了昵称导致定位不到、页面结构变化。\n\n"
            "如果解决不了，先手动发一条保住火花，再慢慢排查。"
        )
        return title, body

    if level is AlertLevel.WARNING:
        title = f"⚠️ 连续 {report.consecutive_failures} 天发送失败"
        body = (
            f"{report.summary()}\n\n"
            "**建议动作**：抽空看一眼工作台的运行历史。"
            "如果明天还失败，会升级为强提醒。"
        )
        return title, body

    # NOTICE：有失败/不确定，或者定时任务的例行汇报
    return _craft_notice_text(
        report=report,
        failed_now=failed_now,
        uncertain_now=uncertain_now,
        force_summary=force_summary,
    )


def _craft_notice_text(
    *,
    report: RunReport,
    failed_now: int,
    uncertain_now: int,
    force_summary: bool,
) -> tuple[str, str]:
    """NOTICE 级别文案：直接告诉用户「有几个人没发出去」，而不是让他自己数。"""
    skipped = sum(1 for o in report.outcomes if o.status is RunStatus.SKIPPED)

    # 全员被「防重复」跳过 —— 这不是「成功 0/N」，别把它说成完成了任务
    if not failed_now and not uncertain_now and skipped and skipped == len(report.outcomes):
        return (
            "⏭ 本轮没有实际发送（今天都已经发过了）",
            "所有收件人今天都已经发送过，定时任务按「防重复」规则跳过了这一轮。\n\n"
            f"{report.summary()}\n\n"
            "想再发一次的话，去工作台「首页」点「立即发送一次」——"
            "手动发送不会被防重复拦住。",
        )

    if failed_now or uncertain_now:
        total = len(report.outcomes)
        succeeded_now = sum(1 for o in report.outcomes if o.status is RunStatus.SUCCESS)

        lines: list[str] = []
        if failed_now:
            names = [o.name for o in report.outcomes if o.status is RunStatus.FAILED]
            shown = "、".join(names[:10]) + ("…" if len(names) > 10 else "")
            lines.append(f"❌ 失败 {failed_now} 人：{shown}")
        if uncertain_now:
            names = [o.name for o in report.outcomes if o.status is RunStatus.UNCERTAIN]
            shown = "、".join(names[:10]) + ("…" if len(names) > 10 else "")
            lines.append(f"❓ 结果不确定 {uncertain_now} 人：{shown}")

        detail = "\n".join(lines) + "\n\n" + report.summary()

        # --- 一个人都没成功：必须一眼看出「今天没续上」+ 先查什么 ---
        #
        # ⚠️ 以前这种情况会落到「有人发送结果不确定」这个标题里 —— 含义模糊，
        #    看不出严重性，也不知道该查哪里（实测被用户指出）。
        #    现在明确说出「0 成功」，并把**登录态**放在第一个排查项：
        #    它是这类全灭事故里最常见的原因，而且修起来最快（扫个码 30 秒）。
        if succeeded_now == 0:
            return (
                f"🔴 今天一个都没发出去（0/{total}）",
                detail + "\n\n**先查登录态**：打开工作台「账号」页看一眼 —— "
                "若显示已过期就点「重新扫码」；"
                "若登录态正常，再去「首页 → 运行历史」看失败原因"
                "（可能是页面结构变化或网络问题）。\n\n"
                "**建议动作**：排查后在工作台「首页」点「立即发送一次」手动补发"
                "（手动发送不会被「今天已发」拦住）。",
            )

        # --- 有人失败：说清楚谁没发出去 ---
        if failed_now:
            return (
                f"❌ {failed_now} 人没发出去",
                detail + "\n\n**建议动作**：打开工作台「首页 → 运行历史」看失败原因；"
                "确认是临时问题时，可在「首页」点「立即发送一次」手动补发"
                "（手动发送不会被「今天已发」拦住）。",
            )

        # --- 只有「结果不确定」：必须和「失败」「登录态失效」明确区分开 ---
        #
        # ⚠️ 这条最容易让人误解。它是「消息很可能已经发出去了，只是页面上没能确认到」，
        #    既不是失败，也不代表登录态有问题 —— 以前统称「有人发送结果不确定」，
        #    用户分不清轻重（实测被指出）。现在标题和正文都写死这一点。
        return (
            f"❓ {uncertain_now} 人结果无法确认（消息可能已发出）",
            detail + "\n\n**这不是发送失败，也不代表登录态失效** —— "
            "只是没能在页面上确认到那一条，而且**不会自动重试**（怕重复发送）。\n\n"
            "**建议动作**：手动看一眼那几位好友的会话，确认收到就不用管；"
            "如果发现确实没发出去，在工作台「首页」点「立即发送一次」补发即可。",
        )

    # 一个都没失败 —— 定时任务的例行汇报（用户要求 09:30 跑完发一次）
    title = f"✅ 每日发送完成：{report.summary()}"
    body = (
        "所有收件人的消息都已发送成功，火花已续上。\n\n"
        f"{report.summary()}\n"
        f"本次共 {len(report.outcomes)} 位收件人。"
    )
    if not force_summary:
        # 非定时任务走到这里意味着「没有失败」——措辞改成中性提示
        title = f"ℹ️ 运行完成：{report.summary()}"
    return title, body


# =============================================================================
# 辅助
# =============================================================================


def _notify_skipped_by_lock(settings: Settings) -> None:
    """定时任务因为「已有任务在运行」被跳过时，**必须推送**。

    ⚠️ 2026-10-21 真实事故：一次崩溃留下的残留锁让每天的定时发送静默跳过，
    只留一行 WARNING —— 用户以为在正常发，实际一整天没人收到消息。
    这种「什么都没发生」的失败，比报错更难发现，必须有通知。
    """
    try:
        Dispatcher.from_settings(settings.notify).dispatch(
            Alert(
                level=AlertLevel.NOTICE,
                title="⏭ 今天的定时发送被跳过了",
                body=(
                    "到点时检测到**另一个任务正在运行**，本次定时任务被跳过 —— "
                    "所以今天可能还没人收到消息。\n\n"
                    "**先看工作台首页的运行状态**：\n"
                    "· 如果确实有任务在跑 → 等它跑完就行；\n"
                    "· 如果显示没有任务在跑（比如刚才异常退出过）→ 多半是残留锁，"
                    "在服务器上执行 `rm -f data/.run.lock`，再回首页点「立即发送一次」。\n\n"
                    "（这个提醒每天最多一条，不会刷屏。）"
                ),
            )
        )
        LOGGER.warning("定时任务被锁跳过，已推送提醒")
    except Exception as exc:  # noqa: BLE001 —— 通知失败不能反过来影响任务
        LOGGER.warning("推送「定时任务被跳过」失败：%s", exc)


class _LockGuard:
    """锁的上下文管理器，拿不到锁时返回 False 而不是抛异常。

    「拿不到锁」和「执行失败」是两种性质不同的情况：
    前者说明另一个任务正在正常工作，不是错误。
    """

    def __init__(self, repo: Repository) -> None:
        self.repo = repo
        self.lock = repo.run_lock()

    def __enter__(self) -> bool:
        try:
            self.lock.acquire()
        except LockBusyError as exc:
            LOGGER.warning("无法获取运行锁：%s", exc)
            return False
        return True

    def touch(self) -> None:
        """刷新锁心跳。长任务（错峰等待、多人发送）里要定期调用。

        不刷新的话，超过 HEARTBEAT_STALE_SECONDS 就会被判成残留锁并被后来者夺走 ——
        那就真的两个任务并跑了。
        """
        try:
            self.lock.touch()
        except Exception as exc:  # noqa: BLE001 —— 心跳失败绝不能影响发送
            LOGGER.debug("刷新锁心跳失败：%s", exc)

    def __exit__(self, *exc_info: object) -> None:
        self.lock.release()


def _acquire_lock(repo: Repository) -> _LockGuard:
    return _LockGuard(repo)


def _default_account_id(repo: Repository) -> str:
    """确定用哪个账号。

    目前只支持单账号，用第一个有登录态的文件名。
    多账号会在将来扩展 —— 那时这里改成从配置读。
    """
    accounts = repo.list_accounts()
    return accounts[0] if accounts else "main"


def _build_plan(
    repo: Repository,
    settings: Settings,
    *,
    contacts_override: tuple[Contact, ...] | None = None,
    messages_override: tuple[Message, ...] | None = None,
    prevent_duplicates: bool = True,
) -> TaskPlan | None:
    """冻结本次要执行的任务计划。

    冻结的意义：任务开始后中途改网页配置不会影响正在跑的这轮，
    避免出现「发给了 A、配置里已经换成了 B」这种不可复现的状态。

    关于 ``contacts_override`` 的语义（刻意的）：

    - **不过滤 ``enabled``**。定时任务只发给启用的联系人；
      但手动触发（工作台「立即发送」、CLI）是用户**明确指定**的 ——
      指定本身就是意图，不该再拿「未启用」拦下来（「我明明选了为什么没发」）。

    ``prevent_duplicates`` 由 ``run_once`` 按触发来源传进来：
    定时任务 True，手动/CLI False。**不要再依据「有没有传 messages_override」
    来推断** —— 那正是「手动点了发送却什么都没发」这个事故的成因。
    """
    targets = contacts_override if contacts_override is not None else repo.enabled_contacts()

    if not targets:
        return None

    if messages_override is not None:
        messages = messages_override
    else:
        messages = repo.load_messages()
        if not messages:
            # 没有配消息时给一条默认的 —— 总比什么都不发好，
            # 而且用户能在工作台看到「用的是默认文案」并去改
            messages = (Message(kind="text", content="早"),)
            LOGGER.warning("消息池为空，使用默认文案「早」。建议在工作台配置文案池。")

    return TaskPlan(
        task_id=new_run_id(),
        targets=targets,
        messages=messages,
        interval=repo.load_interval(),
        prevent_duplicates=prevent_duplicates,
        continue_on_error=True,
    )


__all__ = [
    "RunAbortedError",
    "RunContext",
    "resolve_prevent_duplicates",
    "run_once",
]
