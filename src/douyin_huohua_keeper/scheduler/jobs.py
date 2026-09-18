"""进程内定时调度。

用 APScheduler 把「每天几点发」变成真实触发，而不是依赖宿主机的 cron。
放在进程内的好处：**网页上改完时间立刻生效**，不需要重载容器、不需要碰 crontab。

关于抖动的实现：不是「在 10:30 触发然后 sleep 随机时间」，而是
**每次重建 job 时随机决定当天的实际触发点**。这样：
- 触发时刻在调度器里就是确切的，不会有一个长期挂着的 sleep
- 服务在 10:31 重启也不会错过（APScheduler 按 next_run_time 判断）
- 日志里看到的时间就是你实际预期的时间

局限性（写在这里以免将来困惑）：抖动只在 job 重建或触发后重新计算，
服务重启会重算一次。这对「每天发一条」的场景完全够用。
"""

from __future__ import annotations

import contextlib
import logging
import random
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..config import Settings, load_settings
from ..models import Schedule
from ..store.repo import repository_for

LOGGER = logging.getLogger(__name__)

# job 的固定 ID —— 重建时用它替换掉旧的，避免积累一堆僵尸 job
JOB_ID = "huohua-daily-send"
DIGEST_JOB_ID = "huohua-daily-digest"


@dataclass(frozen=True, slots=True)
class SchedulerStatus:
    """调度器状态，工作台直接展示这个。"""

    running: bool
    enabled: bool
    description: str
    next_run_at: str | None = None
    next_run_in_seconds: float | None = None
    job_count: int = 0


class Scheduler:
    """包装 APScheduler 的 BackgroundScheduler。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or load_settings()
        self._scheduler: Any = None
        self._callback: Any = _default_callback
        self._digest_callback: Any = _default_digest_callback

    # --- 生命周期 -----------------------------------------------------------

    def start(self, *, run_callback: Any = None, digest_callback: Any = None) -> None:
        """启动调度器并注册任务。

        ``run_callback`` / ``digest_callback`` 允许注入替身，便于测试。
        """
        from apscheduler.schedulers.background import BackgroundScheduler

        if self._scheduler is not None:
            LOGGER.debug("调度器已在运行")
            return

        if not self.settings.scheduler.enabled:
            LOGGER.info("调度器已禁用（HUOHUA_ENABLE_SCHEDULER=false），跳过启动")
            return

        timezone = resolve_timezone(self.settings.scheduler.schedule.timezone)
        self._scheduler = BackgroundScheduler(
            timezone=timezone,
            job_defaults={
                "coalesce": True,  # 错过多次触发只补跑一次
                "max_instances": 1,  # 绝不并发跑两次  ← 和文件锁形成双保险
                "misfire_grace_time": 3600,  # 错过 1 小时内还能补
            },
        )
        self._scheduler.start()

        self._callback = run_callback or _default_callback
        self._digest_callback = digest_callback or _default_digest_callback
        self.reschedule()

        LOGGER.info(
            "调度器已启动：%s（时区 %s）",
            self._schedule_from_config().describe(),
            timezone,
        )

        # 兜底检查「今天是不是被静默跳过了」。
        # ⚠️ 放后台线程：通知要发 HTTP（还可能重试），不能拖住工作台启动。
        threading.Thread(
            target=_warn_if_today_missed,
            args=(self._schedule_from_config(),),
            kwargs={"settings": self.settings},
            name="huohua-missed-check",
            daemon=True,
        ).start()

    def shutdown(self, *, wait: bool = False) -> None:
        if self._scheduler is None:
            return
        try:
            self._scheduler.shutdown(wait=wait)
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("关闭调度器失败：%s", exc)
        finally:
            self._scheduler = None

    def __enter__(self) -> Scheduler:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.shutdown()

    # --- 任务管理 -----------------------------------------------------------

    def reschedule(self, schedule: Schedule | None = None) -> None:
        """（重新）注册任务。改完配置后调用即可生效。

        每次调用都会先移除旧 job —— 否则会有多个 job 同时触发。

        **配置来源优先级：``tasks.json`` 优先，环境变量只作兜底默认值。**

        这一点必须统一。工作台改时间写的是 ``tasks.json``，而环境变量在进程
        启动时就固定了；如果这里用环境变量，就会出现在
        「工作台改了时间 → 重启服务 → 第一次跑还是老时间 → 跑完之后才变成新时间」
        的错乱（实测踩过：首次排程用 env 的 10:30，跑完一次后重排又跳到
        tasks.json 的 09:15）。同一个配置有两个来源，迟早会不一致。
        """
        if self._scheduler is None:
            return

        schedule = schedule or self._schedule_from_config()

        # 移除旧的 —— job 不存在是正常情况
        with contextlib.suppress(Exception):
            self._scheduler.remove_job(JOB_ID)

        if not schedule.enabled:
            LOGGER.info("定时发送已关闭，未注册任务")
            return

        timezone = resolve_timezone(schedule.timezone)

        # 算出今天（或明天）的实际触发时刻
        next_run = _next_trigger(schedule, timezone)

        self._scheduler.add_job(
            self._callback,
            trigger="date",
            run_date=next_run,
            id=JOB_ID,
            name="抖音火花每日续期",
            replace_existing=True,
            timezone=timezone,
        )

        LOGGER.info(
            "已排定下次运行：%s（%s）",
            next_run.strftime("%Y-%m-%d %H:%M:%S"),
            schedule.timezone,
        )

        # 每日汇总（默认 23:30）也一起排上
        self._schedule_digest(timezone)

    def _schedule_digest(self, timezone: ZoneInfo) -> None:
        """排定「每日汇总推送」。

        为什么需要它：单次告警只描述某一轮。夜里这条把「今天一共怎么样」
        一次性说清楚 —— 用户明确要求「晚上十一点半也发一次」。

        没有任何通知通道时直接跳过（排了也没人能收到）。
        """
        if self._scheduler is None:
            return

        with contextlib.suppress(Exception):
            self._scheduler.remove_job(DIGEST_JOB_ID)

        notify = self.settings.notify
        if not notify.digest_enabled:
            LOGGER.info("每日汇总已关闭（HUOHUA_DIGEST_ENABLED=false）")
            return
        if not notify.enabled():
            # 没配通道 —— 不用排，免得每天白跑一次
            return

        next_run = _next_daily_trigger(notify.digest_hour, notify.digest_minute, timezone)

        self._scheduler.add_job(
            self._digest_callback,
            trigger="date",
            run_date=next_run,
            id=DIGEST_JOB_ID,
            name="每日汇总推送",
            replace_existing=True,
            timezone=timezone,
        )

        LOGGER.info("已排定每日汇总：%s", next_run.strftime("%Y-%m-%d %H:%M:%S"))

    def _schedule_from_config(self) -> Schedule:
        """读取当前生效的时间配置：优先 ``tasks.json``，读不到再退回环境变量。"""
        try:
            return repository_for(self.settings).load_schedule()
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("读取 tasks.json 里的时间失败（%s），回退到环境变量", exc)
            return self.settings.scheduler.schedule

    def trigger_now(self) -> None:
        """立即触发一次（工作台的「立即发送」按钮）。

        走 ``_callback`` 而不是绕过它 —— 保证手动触发和定时触发走同一条路径，
        包括文件锁。这样「手动点的时候定时任务正好也在跑」不会撞车。
        """
        if self._scheduler is not None:
            callback = getattr(self, "_callback", _default_callback)
        else:
            callback = _default_callback

        callback()

    # 兼容别名
    run_now = trigger_now

    # --- 状态 ---------------------------------------------------------------

    def status(self) -> SchedulerStatus:
        # 与 reschedule 用同一个来源，否则「界面显示的时间」和「实际排程的时间」
        # 会不一致 —— 那种不一致用户根本查不出来。
        schedule = self._schedule_from_config()

        if self._scheduler is None:
            return SchedulerStatus(
                running=False,
                enabled=schedule.enabled and self.settings.scheduler.enabled,
                description=schedule.describe(),
            )

        jobs = self._scheduler.get_jobs()
        target = next((j for j in jobs if j.id == JOB_ID), None)

        next_run_at = None
        next_in = None
        if target is not None and target.next_run_time is not None:
            next_dt = target.next_run_time
            next_run_at = next_dt.strftime("%Y-%m-%d %H:%M:%S")
            try:
                next_in = max(0.0, (next_dt - datetime.now(ZoneInfo(str(next_dt.tzinfo)))).total_seconds())
            except Exception:  # noqa: BLE001
                next_in = None

        return SchedulerStatus(
            running=True,
            enabled=schedule.enabled,
            description=schedule.describe(),
            next_run_at=next_run_at,
            next_run_in_seconds=next_in,
            job_count=len(jobs),
        )

    def describe_next(self) -> str:
        """给日志和网页用的一句话：「下次运行还有 3 小时 12 分」。"""
        status = self.status()

        if not status.running:
            return "调度器未运行"
        if not status.enabled:
            return "定时发送已关闭"
        if status.next_run_at is None:
            return "未排定运行时间"
        if status.next_run_in_seconds is None:
            return f"下次运行：{status.next_run_at}"

        return f"下次运行：{status.next_run_at}（还有 {_humanize(status.next_run_in_seconds)}）"


# =============================================================================
# 内部
# =============================================================================


def _notify_scheduled_not_run(reason: str) -> None:
    """「今天这一轮压根没执行」时必须通知 —— 这是最危险的一类静默失败。

    为什么单独做这个：``run_once`` 返回 ``None`` 表示「没有执行」
    （没有启用的收件人/消息，或另一个任务占着运行锁）。这种情况下
    **既没有报告、也没有告警**，用户只会以为「今天已经发过了」——
    而实际上一个消息都没发出去。
    """
    try:
        from ..notify import Alert, AlertLevel, Dispatcher

        settings = load_settings()
        dispatcher = Dispatcher.from_settings(settings.notify)
        alert = Alert(
            level=AlertLevel.NOTICE,
            title="⚠️ 今天的自动发送没有执行",
            body=(
                f"{reason}\n\n"
                "**建议动作**：打开工作台「首页」，点「立即发送一次」手动补发"
                "（手动发送不会被「今天已发」拦住）。"
            ),
        )
        result = dispatcher.dispatch(alert)
        LOGGER.info("「未执行」通知结果：%s", result.summary())
    except Exception:
        LOGGER.exception("发送「未执行」通知失败")


def _default_callback() -> None:
    """默认的触发回调：跑一次任务，跑完把下一次排上。

    因为没有用 cron 触发器（那样抖动不好实现），所以每次跑完要手动
    排下一次。这也是为什么这个回调必须自己再调一次 ``reschedule``。
    """
    from .runner import run_once

    try:
        # 定时任务是**唯一**开启「当日防重复」的场景；
        # 手动触发一律真发（见 runner.run_once 的 prevent_duplicates 说明）
        report = run_once(kind="scheduled", prevent_duplicates=True)
        if report is not None:
            LOGGER.info("定时任务完成：%s", report.summary())
        else:
            # ⚠️ 绝不能静默：用户会以为今天已经发过了
            LOGGER.error("定时任务没有执行（没有启用的收件人/消息，或已有任务在跑）")
            _notify_scheduled_not_run(
                "定时任务的时间到了，但**一个收件人都没有发**。"
                "常见原因：没有启用的收件人、消息池为空，或另一个任务正占着运行锁。"
            )
    except Exception:
        LOGGER.exception("定时任务执行失败")
        _notify_scheduled_not_run("定时任务执行时抛了异常，详见服务端日志。")
    finally:
        # 排下一天
        try:
            settings = load_settings()
            schedule = repository_for(settings).load_schedule()
            _reschedule_from_callback(settings, schedule)
        except Exception:
            LOGGER.exception("排定下次运行失败 —— 服务重启后会重新排定")


def _warn_if_today_missed(
    schedule: Schedule,
    *,
    settings: Settings | None = None,
    repo: Any = None,
    now: datetime | None = None,
) -> None:
    """启动时检查「今天是不是被静默跳过了」。

    场景：服务器在 09:30 那一刻**没在运行**（宕机 / 重启 / 断电）。
    重启后 ``reschedule()`` 算出来的下一次触发是**明天** —— 于是今天
    就被静默跳过了：没有任何报告、没有任何通知，用户以为发过了。

    对一个「火花不能断」的任务，这种跳过必须让用户知道。
    每天最多提醒一次（落一个日期戳），所以反复重启也不会重复打扰。
    """
    try:
        settings = settings or load_settings()
        repo = repo or repository_for(settings)

        if not schedule.enabled:
            return

        timezone = resolve_timezone(schedule.timezone)
        # now 可注入：测试需要固定时间，否则「今天 00:00 已过」这种断言会随运行时刻漂移
        now = now or datetime.now(timezone)
        today_at = now.replace(hour=schedule.hour, minute=schedule.minute, second=0, microsecond=0)
        # 给正在跑的那一轮留 15 分钟余量，避免「09:32 重启」被误判成漏发
        if now < today_at + timedelta(minutes=15):
            return
        if repo.today_succeeded():
            return

        stamp = repo.config_dir / ".last_missed_notice"
        today = now.strftime("%Y-%m-%d")
        with contextlib.suppress(OSError):
            if stamp.read_text(encoding="utf-8").strip() == today:
                return  # 今天已经提醒过，不重复打扰

        LOGGER.warning("今天 %s 的自动发送没有成功记录，提醒用户", today_at.strftime("%H:%M"))
        _notify_scheduled_not_run(
            f"今天 {schedule.hour:02d}:{schedule.minute:02d} 的自动发送**没有成功执行**。"
            "服务在那个时间点没有在运行（或那一轮失败了）。"
        )
        with contextlib.suppress(OSError):
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.write_text(today, encoding="utf-8")
    except Exception:
        LOGGER.exception("检查「今天是否漏发」失败（不影响启动）")


_scheduler_singleton: Scheduler | None = None


def _reschedule_from_callback(settings: Settings, schedule: Schedule) -> None:
    global _scheduler_singleton
    if _scheduler_singleton is None:
        return
    _scheduler_singleton.settings = settings
    _scheduler_singleton.reschedule(schedule)


def _default_digest_callback() -> None:
    """每日汇总的默认触发回调。

    跑完自己再排下一天（这里用的是 ``date`` 触发器，没有 cron 的自动重复，
    和发送任务同理）。
    """
    try:
        settings = load_settings()
        _send_daily_digest(settings, repository_for(settings))
    except Exception:
        LOGGER.exception("发送每日汇总失败")
    finally:
        try:
            settings = load_settings()
            schedule = repository_for(settings).load_schedule()
            _reschedule_digest_from_callback(settings, schedule)
        except Exception:
            LOGGER.exception("排定下次每日汇总失败 —— 服务重启后会重新排定")


def _reschedule_digest_from_callback(settings: Settings, schedule: Schedule) -> None:
    """汇总跑完后重排（连同发送任务一起重排，保证两者都被排上）。"""
    global _scheduler_singleton
    if _scheduler_singleton is None:
        return
    _scheduler_singleton.settings = settings
    _scheduler_singleton.reschedule(schedule)


def _send_daily_digest(settings: Settings, repo: Any) -> None:
    """把「今天整体怎么样」推给用户（默认 23:30）。"""
    from ..notify import Alert, AlertLevel, Dispatcher
    from ..store.repo import today_str

    entry = repo.daily_entry() or {}
    schedule = repo.load_schedule()

    ok = bool(entry.get("success"))
    attempts = int(entry.get("attempts") or 0)
    sent_to = list(entry.get("sent_to") or [])
    last = entry.get("last_summary") or "今天没有任何运行记录"

    if ok:
        title = "🌙 今日汇总：火花已续上"
        body = (
            f"日期：{today_str()}\n"
            f"今天共运行 {attempts} 次\n"
            f"成功发给：{'、'.join(sent_to) if sent_to else '（未记录具体名单）'}\n"
            f"最近一次：{last}\n\n"
            f"下次自动发送：{schedule.describe()}"
        )
    else:
        title = "🌙 今日汇总：今天没有成功发送" if attempts else "🌙 今日汇总：今天没有运行"
        body = (
            f"日期：{today_str()}\n"
            f"今天共运行 {attempts} 次，但没有一次成功。\n"
            f"最近一次：{last}\n\n"
            "**建议动作**：打开工作台「首页 → 运行历史」看失败原因，"
            "然后在首页点「立即发送一次」手动补发（手动发送不会被「今天已发」拦住）。\n"
            "如果提示登录态失效，去「账号」页重新扫码（可在「登录页操作」里完成二次验证）。"
        )

    dispatcher = Dispatcher.from_settings(settings.notify)
    report = dispatcher.dispatch(Alert(level=AlertLevel.NOTICE, title=title, body=body))
    LOGGER.info("每日汇总分发结果：%s", report.summary())


def _next_daily_trigger(hour: int, minute: int, timezone: ZoneInfo) -> datetime:
    """算出下一次「每天 hour:minute」的确切时刻（已过则顺延到明天）。"""
    now = datetime.now(timezone)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def set_singleton(scheduler: Scheduler | None) -> None:
    """注册全局调度器实例，让回调能重新排定自己。"""
    global _scheduler_singleton
    _scheduler_singleton = scheduler


def get_singleton() -> Scheduler | None:
    return _scheduler_singleton


def resolve_timezone(name: str, *, strict: bool = False) -> ZoneInfo:
    """解析时区名。

    ``strict=False``（默认）失败时退回本地时区并警告 —— 服务得能起来，
    不能因为一个写错的时区名就拒绝启动。

    ``strict=True`` 失败时直接抛异常。工作台保存配置时用这个 ——
    时区写错必须当场发现，因为火花按自然日算，8 小时偏差会直接导致漏发，
    而「保存成功了但其实是错的」是最糟的结果。

    故意**不**静默用 UTC —— 那会让发送窗口偏 8 小时。退回本地时区至少
    和运行环境一致。
    """
    try:
        return ZoneInfo(name)
    except Exception as exc:
        if strict:
            raise ValueError(f"无法解析时区 {name!r}。请使用 IANA 名称，例如 Asia/Shanghai。") from exc

        local_now = datetime.now().astimezone()
        fallback = local_now.tzinfo or ZoneInfo("UTC")
        LOGGER.warning(
            "无法解析时区 %r，退回系统本地时区（当前时间 %s）。"
            "请检查 HUOHUA_TZ / TZ 设置，避免发送窗口偏移导致漏发。",
            name,
            local_now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        )
        return fallback  # type: ignore[return-value]


def _next_trigger(schedule: Schedule, timezone: ZoneInfo) -> datetime:
    """算出下一次应该触发的确切时刻。

    在 ``hour:minute`` 基础上加一个 ``0..jitter_minutes`` 的随机偏移。
    如果算出来的时刻已经过去，就顺延到明天。
    """
    now = datetime.now(timezone)
    jitter = random.randint(0, schedule.jitter_minutes) if schedule.jitter_minutes > 0 else 0

    candidate = now.replace(
        hour=schedule.hour,
        minute=schedule.minute,
        second=0,
        microsecond=0,
    ) + timedelta(minutes=jitter)

    # 已经过了就排明天（同样带抖动）
    if candidate <= now:
        jitter = random.randint(0, schedule.jitter_minutes) if schedule.jitter_minutes > 0 else 0
        candidate = (now + timedelta(days=1)).replace(
            hour=schedule.hour,
            minute=schedule.minute,
            second=0,
            microsecond=0,
        ) + timedelta(minutes=jitter)

    return candidate


def _humanize(seconds: float) -> str:
    """把秒数变成「3 小时 12 分」这样的人话。"""
    seconds = int(max(0, seconds))

    if seconds < 60:
        return f"{seconds} 秒"

    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分" + (f" {sec} 秒" if sec else "")

    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} 小时" + (f" {minutes} 分" if minutes else "")

    days, hours = divmod(hours, 24)
    return f"{days} 天" + (f" {hours} 小时" if hours else "")


__all__ = [
    "DIGEST_JOB_ID",
    "JOB_ID",
    "Scheduler",
    "SchedulerStatus",
    "get_singleton",
    "resolve_timezone",
    "set_singleton",
]
