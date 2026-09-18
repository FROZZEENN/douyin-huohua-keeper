"""进程内定时调度。

用 APScheduler 把「每天几点发」变成真实触发，而不是依赖宿主机的 cron。
放在进程内的好处：网页上改完时间立刻生效，不需要重载容器、不需要碰 crontab。

- ``runner``  —— 单次执行编排：加锁 → 冻结计划 → 逐个收件人发送 → 落盘 → 告警
- ``jobs``    —— APScheduler 封装，任务的注册、重建、状态查询

这一层负责「决策」：该不该重试、要不要升级告警、连续失败到第几次了。
它调用 ``engine`` 做事，读 ``store`` 拿状态，写 ``store`` 存结果，通过
``notify`` 出声。
"""

from __future__ import annotations

from .jobs import (
    JOB_ID,
    Scheduler,
    SchedulerStatus,
    get_singleton,
    set_singleton,
)
from .runner import RunAbortedError, RunContext, run_once

__all__ = [
    "JOB_ID",
    "RunAbortedError",
    "RunContext",
    "Scheduler",
    "SchedulerStatus",
    "get_singleton",
    "run_once",
    "set_singleton",
]
