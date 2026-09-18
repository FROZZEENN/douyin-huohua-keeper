"""系统状态：概览、自检、日志、通知测试。

这一组接口的共同点是**只读或低风险**，除了「测试通知」会真的发一条消息出去。

设计取舍：``/api/system/overview`` 是前端首屏唯一的聚合接口 ——
把配置、调度、账号、今天的战果一次全给你，避免前端开屏打五六个请求。
（健康检查 ``/api/health`` 是另一个极端，刻意保持极简，因为 Docker 会高频打它。）
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ... import __version__, runstate
from ...engine.auth import summarize_state
from ...store.repo import now_str, today_str

LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/api/system", tags=["system"])

# 读日志尾部时最多往回读多少字节。日志可能很大，不能整个读进来。
LOG_TAIL_MAX_BYTES = 256 * 1024
LOG_TAIL_DEFAULT_LINES = 200


def _state(request: Request) -> Any:
    return request.app.state.keeper


# =============================================================================
# 概览
# =============================================================================


@router.get("/overview")
async def overview(request: Request) -> dict[str, Any]:
    """首屏聚合状态。

    一个接口给全：账号、调度、今天的战果、收件人、配置摘要、告警问题。
    这是前端唯一会「一次拿很多」的接口 —— 首屏多等 100ms 好过开屏闪五次。
    """
    state = _state(request)
    settings = state.settings
    repo = state.repository

    account_id = _current_account_id(repo)
    account_state = summarize_state(repo.account_state_path(account_id))

    contacts = repo.load_contacts()
    today = today_str()
    bundle = repo.load_task_bundle()
    messages = bundle["messages"]
    schedule = bundle["schedule"]
    streak = repo.load_streak_state()
    # daily_index.json 只读一次（原来 today_succeeded + daily_entry 各读一遍）
    daily_entry = repo.daily_entry()

    return {
        "version": __version__,
        "now": now_str(),
        "today": today,
        "account": {
            "id": account_id,
            "state": account_state,
            "engine_running": state.peek_engine() is not None,
        },
        "scheduler": _scheduler_payload(state),
        "schedule": {
            "enabled": schedule.enabled,
            "hour": schedule.hour,
            "minute": schedule.minute,
            "jitter_minutes": schedule.jitter_minutes,
            "timezone": schedule.timezone,
            "describe": schedule.describe(),
        },
        "contacts": {
            "total": len(contacts),
            "enabled": sum(1 for c in contacts if c.enabled),
            "sent_today": sum(1 for c in contacts if c.sent_today(today)),
        },
        "messages": {
            "count": len(messages),
            "empty": not messages,
            "kinds": sorted({m.kind for m in messages}),
        },
        "today_result": {
            "success": bool((daily_entry or {}).get("success")),
            "entry": daily_entry,
        },
        "streak": {
            "consecutive_successes": int(streak.get("consecutive_successes") or 0),
            "consecutive_failures": int(streak.get("consecutive_failures") or 0),
            "streak_start_date": streak.get("streak_start_date"),
            "last_success_date": streak.get("last_success_date"),
        },
        "config": {
            "dry_run": settings.send.dry_run,
            "headless": settings.browser.headless,
            "browser": settings.browser.describe(),
            "send": settings.send.describe(),
            "notify_channels": list(settings.notify.channels),
            "notify_describe": settings.notify.describe(),
            "scheduler_enabled": settings.scheduler.enabled,
            "data_dir": str(settings.data_dir.resolve()),
            "log_dir": str(settings.log_dir.resolve()),
            "log_level": settings.log_level,
            "token_set": bool(settings.workbench.token),
            "allowed_ips": list(settings.workbench.allowed_ips),
        },
        "problems": settings.validate(),
        "manual_run_running": _manual_running(),
    }


@router.get("/run-progress")
async def run_progress() -> dict[str, Any]:
    """当前运行任务的实时进度（首页「任务进行状态」轮询它）。

    返回 :mod:`douyin_huohua_keeper.runstate` 的快照；没有任务在跑时
    ``active`` 为 ``False``。这个接口只读、无副作用、极轻量，可以高频轮询。
    """
    return runstate.snapshot()


@router.get("/config")
async def system_config(request: Request) -> dict[str, Any]:
    """当前生效的配置（不含敏感值）。"""
    state = _state(request)
    settings = state.settings

    return {
        "describe": settings.describe(),
        "problems": settings.validate(),
        "browser": {
            "headless": settings.browser.headless,
            "describe": settings.browser.describe(),
            "nav_timeout_ms": settings.browser.nav_timeout_ms,
            "action_timeout_ms": settings.browser.action_timeout_ms,
        },
        "send": {
            "describe": settings.send.describe(),
            "dry_run": settings.send.dry_run,
            "max_retries": settings.send.max_retries,
            "retry_backoff_sec": settings.send.retry_backoff_sec,
            "risk_cooldown_sec": settings.send.risk_cooldown_sec,
            "confirm_timeout_ms": settings.send.confirm_timeout_ms,
        },
        "notify": {
            "channels": list(settings.notify.channels),
            "describe": settings.notify.describe(),
            "warn_threshold": settings.notify.warn_threshold,
            "critical_threshold": settings.notify.critical_threshold,
            # 只说「配了没有」，绝不含 webhook URL / token 本身
            "configured": {
                name: _channel_configured(settings.notify, name) for name in settings.notify.KNOWN_CHANNELS
            },
        },
        "scheduler": {
            "enabled": settings.scheduler.enabled,
            "run_once": settings.scheduler.run_once,
            "describe": settings.scheduler.describe(),
        },
        "workbench": {
            "host": settings.workbench.host,
            "port": settings.workbench.port,
            "token_set": bool(settings.workbench.token),
            "allowed_ips": list(settings.workbench.allowed_ips),
        },
        "paths": {
            "data_dir": str(settings.data_dir.resolve()),
            "log_dir": str(settings.log_dir.resolve()),
        },
    }


# =============================================================================
# 自检
# =============================================================================


@router.post("/check")
def run_check(request: Request) -> dict[str, Any]:
    """在工作台里跑一次轻量自检。

    和 ``scripts/healthcheck.py`` 的关系：那个脚本更全面（含文件系统写权限、
    Chromium 安装等），这个只做「不启动浏览器就能查」的部分 ——
    因为它是在 HTTP 请求里跑的，不能卡住几秒。

    返回结构对齐 healthcheck 的风格：每项有 name / ok / detail / hint。
    """
    state = _state(request)
    settings = state.settings
    repo = state.repository

    checks: list[dict[str, Any]] = []

    # 1. 数据目录可写
    try:
        repo.ensure_layout()
        probe = repo.data_dir / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(_ok("数据目录可写", str(repo.data_dir.resolve())))
    except Exception as exc:  # noqa: BLE001
        checks.append(
            _fail(
                "数据目录可写",
                f"{type(exc).__name__}: {exc}",
                "检查目录权限；容器里确认卷挂载正确、uid 10001 有写权限。",
            )
        )

    # 2. 令牌
    if settings.workbench.token:
        if len(settings.workbench.token) >= 12:
            checks.append(_ok("访问令牌", f"已设置（{len(settings.workbench.token)} 字符）"))
        else:
            checks.append(
                _warn(
                    "访问令牌",
                    f"已设置但偏短（{len(settings.workbench.token)} 字符）",
                    "建议至少 24 位随机字符。",
                )
            )
    else:
        checks.append(
            _fail(
                "访问令牌",
                "未设置 —— 工作台无鉴权开放",
                "设置 HUOHUA_TOKEN，否则任何能访问端口的人都能操作。",
            )
        )

    # 3. 登录态（纯本地）
    account_id = _current_account_id(repo)
    account_state = summarize_state(repo.account_state_path(account_id))
    if account_state["present"] and not account_state["expired"]:
        checks.append(_ok("登录态文件", account_state["detail"]))
    elif account_state["present"]:
        checks.append(_fail("登录态文件", account_state["detail"], "去「账号」页重新扫码登录。"))
    else:
        checks.append(
            _warn(
                "登录态文件", account_state["detail"], "这是首次部署的正常状态 —— 去「账号」页扫码绑定即可。"
            )
        )

    # 4. 收件人
    contacts = repo.load_contacts()
    enabled = [c for c in contacts if c.enabled]
    if enabled:
        checks.append(_ok("收件人", f"{len(enabled)} 个启用（共 {len(contacts)} 个）"))
    else:
        checks.append(_warn("收件人", "没有启用的收件人", "去「收件人」页添加，或从会话列表同步。"))

    # 5. 消息池
    messages = repo.load_messages()
    if messages:
        checks.append(_ok("消息池", f"{len(messages)} 条"))
    else:
        checks.append(_warn("消息池", "为空，发送时会用默认文案「早」", "去「任务」页配置几条常用文案。"))

    # 6. 通知通道
    #
    # ⚠️ 不能只看「配置项在不在」就判定通过 —— 配置齐全但实际发不出去
    #    （密钥过期、网络不通、代码路径有 bug）才是最该被自检抓出来的情况。
    #    回归：以前这里只校验配置完整性，于是「通知明明发不出去，自检照样通过」。
    #    现在只要配了通道就真发一条探测消息，发不出去即判不通过。
    notify_problems = settings.notify.validate()
    if not settings.notify.channels:
        checks.append(
            _warn("通知通道", "未配置 —— 失败时不会通知你", "至少配一个（Bark / 钉钉 / 飞书 / Telegram）。")
        )
    elif notify_problems:
        checks.append(_fail("通知通道", "；".join(notify_problems), "按提示补上对应的环境变量。"))
    else:
        checks.append(_probe_notify(settings.notify))

    # 7. 调度器
    sched = _scheduler_payload(state)
    if sched.get("running"):
        checks.append(_ok("调度器", sched.get("human") or "运行中"))
    elif settings.scheduler.enabled:
        checks.append(
            _warn("调度器", sched.get("detail") or "未运行", "确认 HUOHUA_ENABLE_SCHEDULER 没被设为 false。")
        )
    else:
        checks.append(_warn("调度器", "已禁用（由外部 cron 触发）", "如果没配 cron，就不会自动发送。"))

    # 8. 配置问题总汇
    problems = settings.validate()
    if problems:
        checks.append(_warn("配置校验", f"{len(problems)} 个问题", "；".join(problems[:3])))
    else:
        checks.append(_ok("配置校验", "通过"))

    # 9. 残留临时文件
    try:
        stale = list(repo.data_dir.glob("**/*.tmp"))
        if stale:
            checks.append(
                _warn(
                    "临时文件", f"发现 {len(stale)} 个残留 .tmp", "通常是历史崩溃留下的；重启服务会自动清理。"
                )
            )
        else:
            checks.append(_ok("临时文件", "无残留"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_warn("临时文件", f"无法检查：{exc}", ""))

    # 10. 日志落盘
    #
    # 工作台的「日志」页读的就是 log_dir —— 不落盘那个页面永远为空，
    # 出问题时也查不到痕迹。回归：曾经完全没有日志配置，日志页恒为空。
    checks.append(_probe_logs(settings))

    failed = sum(1 for c in checks if c["status"] == "fail")
    warned = sum(1 for c in checks if c["status"] == "warn")

    return {
        "checks": checks,
        "summary": {
            "total": len(checks),
            "ok": len(checks) - failed - warned,
            "warn": warned,
            "fail": failed,
            "healthy": failed == 0,
        },
        "checked_at": now_str(),
    }


# =============================================================================
# 通知测试
# =============================================================================


class NotifyTestRequest(BaseModel):
    """测试通知。"""

    message: str | None = Field(default=None, max_length=500, description="自定义正文，留空用默认文案")


@router.post("/notify/test")
def notify_test(request: Request, payload: NotifyTestRequest | None = None) -> dict[str, Any]:
    """发一条测试通知。

    这是唯一会主动向外部发消息的接口 —— 所以它的返回体里必须带上
    「哪个通道成功了、哪个失败了」，失败时还要有原因。
    只说一句「发送成功」而实际全挂掉，是最坏的体验。
    """
    payload = payload or NotifyTestRequest()
    state = _state(request)

    # AlertLevel 定义在 models 里 —— 从它自己的家导入，别绕道 notify 包。
    # （回归：这里曾经 ``from ...notify import AlertLevel``，而 notify 包当时并未
    #  导出它 → ImportError → 接口 500，且日志里什么都没有。）
    from ...models import AlertLevel
    from ...notify import Alert, Dispatcher

    settings = state.settings

    if not settings.notify.channels:
        return {
            "ok": False,
            "detail": "没有配置任何通知通道",
            "hint": "在 .env 里设置 HUOHUA_NOTIFY_CHANNELS 以及对应通道的凭据。",
            "results": [],
        }

    if payload.message:
        alert = Alert(
            level=AlertLevel.WARNING,
            title="🔔 测试通知",
            body=payload.message,
        )
    else:
        alert = Alert(
            level=AlertLevel.WARNING,
            title="🔔 测试通知",
            body=(
                "这是一条来自 douyin-huohua-keeper 的测试通知。\n\n"
                "看到这条消息说明通知通道配置正确，"
                "以后出现连续失败或登录态失效时你会收到提醒。"
            ),
        )

    # minimum_level 传 WARNING —— 否则 WARNING 级别的测试消息会被「默认静默」规则拦掉，
    # 用户点了按钮却什么都没发生
    try:
        dispatcher = Dispatcher.from_settings(settings.notify)
        report = dispatcher.dispatch(alert, minimum_level=AlertLevel.WARNING)
    except Exception as exc:
        # 走到这里说明是程序内部错误（不是通道本身发不出去）。
        # 绝不能让它变成「500 + 日志空白」——那是这次踩过的坑。
        LOGGER.exception("发送测试通知失败")
        return {
            "ok": False,
            "detail": f"发送测试通知时发生内部错误：{type(exc).__name__}: {exc}",
            "hint": "这不是通道配置问题，而是程序自身出错。日志里已记录堆栈，可据此排查。",
            "results": [],
            "failed_channels": [],
            "succeeded_channels": [],
        }

    LOGGER.info("测试通知结果：%s", report.summary())

    results = [
        {
            "channel": r.channel,
            "ok": r.ok,
            "detail": r.detail,
            "attempt": r.attempt,
        }
        for r in report.attempted
    ]

    return {
        "ok": report.any_succeeded,
        "detail": report.summary(),
        "results": results,
        "failed_channels": list(report.failed_channels),
        "succeeded_channels": list(report.succeeded_channels),
    }


# =============================================================================
# 日志
# =============================================================================


@router.get("/logs")
async def logs(
    request: Request,
    lines: int = LOG_TAIL_DEFAULT_LINES,
    file: str | None = None,
) -> dict[str, Any]:
    """读日志尾部。

    安全边界：只允许读 ``log_dir`` 里的文件，且只读尾部若干字节。
    传 ``file`` 时做路径穿越检查 —— 这个接口如果不设防，``../../etc/passwd``
    就能读任意文件。
    """
    lines = max(1, min(lines, 5000))
    state = _state(request)
    log_dir = Path(state.settings.log_dir).resolve()

    target = _resolve_log_file(log_dir, file)

    if target is None or not target.is_file():
        return {
            "file": target.name if target else None,
            "lines": [],
            "available": _list_log_files(log_dir),
            "detail": "日志文件不存在",
        }

    try:
        text = _tail(target, LOG_TAIL_MAX_BYTES)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"读取日志失败：{type(exc).__name__}: {exc}",
        ) from exc

    tail = text.splitlines()[-lines:]

    return {
        "file": target.name,
        "lines": tail,
        "line_count": len(tail),
        "size_bytes": target.stat().st_size,
        "modified_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(target.stat().st_mtime)),
        "available": _list_log_files(log_dir),
        "truncated_from_start": len(text) >= LOG_TAIL_MAX_BYTES,
    }


@router.get("/logs/files")
async def log_files(request: Request) -> dict[str, Any]:
    """有哪些日志文件可选。"""
    state = _state(request)
    log_dir = Path(state.settings.log_dir).resolve()
    return {"files": _list_log_files(log_dir), "log_dir": str(log_dir)}


# =============================================================================
# 内部
# =============================================================================


def _current_account_id(repo: Any) -> str:
    accounts = repo.list_accounts()
    return accounts[0] if accounts else "main"


def _scheduler_payload(state: Any) -> dict[str, Any]:
    scheduler = state.scheduler
    if scheduler is None:
        from ...scheduler.jobs import get_singleton

        scheduler = get_singleton()

    if scheduler is None:
        return {
            "running": False,
            "detail": "调度器未启动",
            "human": "调度器未启动",
        }

    status = scheduler.status()
    return {
        "running": status.running,
        "enabled": status.enabled,
        "description": status.description,
        "next_run_at": status.next_run_at,
        "next_run_in_seconds": status.next_run_in_seconds,
        "job_count": status.job_count,
        "human": scheduler.describe_next(),
    }


def _manual_running() -> bool:
    """手动任务是否在跑。避免 system 模块直接依赖 runs 模块的内部变量。"""
    try:
        from . import runs

        return bool(runs._manual_running)
    except Exception:  # noqa: BLE001
        return False


def _channel_configured(notify_settings: Any, channel: str) -> bool:
    required = notify_settings.REQUIRED_FIELDS.get(channel, ())
    return all(bool(getattr(notify_settings, field, "")) for field in required)


def _resolve_log_file(log_dir: Path, name: str | None) -> Path | None:
    """解析要读的日志文件，挡住路径穿越。

    步骤：候选路径 → resolve() → 确认它仍在 log_dir 之内。
    没有 ``file`` 时取最近修改的那个日志文件。
    """
    if name:
        # 只取文件名部分，把任何目录成分丢掉 —— 这是最省事也最可靠的做法
        candidate = (log_dir / Path(name).name).resolve()
        if not _is_within(candidate, log_dir):
            LOGGER.warning("拒绝读取日志目录外的文件：%s", name)
            return None
        return candidate

    files = _list_log_files(log_dir)
    if not files:
        return None
    newest = max(files, key=lambda item: item["modified_at"])
    return log_dir / newest["name"]


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _list_log_files(log_dir: Path) -> list[dict[str, Any]]:
    if not log_dir.is_dir():
        return []

    items: list[dict[str, Any]] = []
    try:
        for path in sorted(log_dir.iterdir()):
            if not path.is_file() or path.suffix not in {".log", ".txt", ".jsonl"}:
                continue
            stat = path.stat()
            items.append(
                {
                    "name": path.name,
                    "size_bytes": stat.st_size,
                    "size_human": _human_size(stat.st_size),
                    "modified_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                }
            )
    except OSError as exc:
        LOGGER.warning("列出日志文件失败：%s", exc)

    return sorted(items, key=lambda item: item["modified_at"], reverse=True)


def _tail(path: Path, max_bytes: int) -> str:
    """从文件尾部读指定字节数，按 utf-8 容错解码。

    不能整个读进来：日志文件滚起来能到几百 MB。
    从尾部读还可能截断一个多字节字符，所以用 ``errors="replace"``
    并把第一个残缺行丢掉 —— 显示一个乱码字符不如丢掉半行。
    """
    size = path.stat().st_size
    read_bytes = min(size, max_bytes)

    with path.open("rb") as handle:
        if size > read_bytes:
            handle.seek(size - read_bytes)
        raw = handle.read()

    text = raw.decode("utf-8", errors="replace")

    if size > read_bytes:
        # 丢掉第一行（可能是被截断的半行）
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1 :]

    return text


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _probe_notify(notify_settings: Any) -> dict[str, Any]:
    """真发一条测试通知，确认通道可达。

    只「看配置项在不在」不够 —— 最该被自检抓出来的恰恰是
    「配置齐全但实际发不出去」。回归：以前只校验配置完整性，
    通知发不出去时自检照样通过。
    """
    from ...models import AlertLevel
    from ...notify import Alert, Dispatcher

    try:
        dispatcher = Dispatcher.from_settings(notify_settings)
        report = dispatcher.dispatch(
            Alert(
                level=AlertLevel.WARNING,
                title="🔔 自检通知",
                body="这条消息由 douyin-huohua-keeper 的「系统自检」发出，用于确认通知通道可达。",
            ),
            minimum_level=AlertLevel.WARNING,
        )
    except Exception as exc:
        LOGGER.exception("自检：通知通道探测出错")
        return _fail(
            "通知通道",
            f"探测时发生内部错误：{type(exc).__name__}: {exc}",
            "这是程序内部错误（非通道配置问题），日志里有堆栈。",
        )

    if report.any_succeeded:
        return _ok("通知通道", f"{report.summary()}（已真发一条测试消息验证）")
    return _fail(
        "通知通道",
        report.summary(),
        "通道配置存在但发送失败，检查密钥 / Webhook 与服务器网络。",
    )


def _probe_logs(settings: Any) -> dict[str, Any]:
    """确认日志真的写进了文件（工作台「日志」页读的就是这里）。"""
    log_dir = Path(settings.log_dir)
    if not log_dir.is_dir():
        return _fail(
            "日志记录",
            f"日志目录不存在：{log_dir}",
            "确认 HUOHUA_LOG_DIR，并让服务以它为日志目录启动。",
        )

    files = [p for p in log_dir.glob("*.log*") if p.is_file()]
    if not files:
        return _fail(
            "日志记录",
            f"{log_dir} 下没有任何日志文件",
            "服务未配置日志落盘，日志页会一直为空。",
        )

    newest = max(files, key=lambda p: p.stat().st_mtime)
    size = newest.stat().st_size
    if size <= 0:
        return _fail("日志记录", f"{newest.name} 存在但内容为空", "确认日志级别与写入权限。")
    return _ok("日志记录", f"{newest.name}（{_human_size(size)}）")


def _ok(name: str, detail: str = "") -> dict[str, Any]:
    return {"name": name, "status": "ok", "ok": True, "detail": detail, "hint": ""}


def _warn(name: str, detail: str = "", hint: str = "") -> dict[str, Any]:
    # ``ok`` 的语义刻意定义为「**不是失败**」而不是「一切正常」：
    # 告警项不该让整体判定为不健康（比如「没配通知通道」不该算自检失败），
    # 但调用方也不该把它当成「没问题」—— 判断「一切正常」要看 ``status == "ok"``。
    return {"name": name, "status": "warn", "ok": True, "detail": detail, "hint": hint}


def _fail(name: str, detail: str = "", hint: str = "") -> dict[str, Any]:
    return {"name": name, "status": "fail", "ok": False, "detail": detail, "hint": hint}
