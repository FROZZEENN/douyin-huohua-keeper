"""运行历史与手动触发。

一次「运行」有两种看法，两个接口分别服务：

- **按天看**（``/api/runs/days``）—— 「这个月有没有断过」。火花是连续的，
  所以这是你最关心的视图
- **按次看**（``/api/runs``）—— 「昨天那次为什么失败了」。排障用

**手动触发**（``POST /api/runs/now``）刻意做成异步的：一次发送要几十秒到
几分钟（浏览器启动 + 错峰间隔），HTTP 请求挂那么久会被各种中间层超时掐断。
所以它立刻返回「已开始」，让你去轮询历史看结果。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ...models import Contact, Message
from ...store.repo import today_str

LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/api/runs", tags=["runs"])

# 手动触发正在跑时的标记。防止连点五次按钮启动五次任务。
_manual_lock = threading.Lock()
_manual_running = False


def _state(request: Request) -> Any:
    return request.app.state.keeper


# =============================================================================
# 按天
# =============================================================================


@router.get("/days")
async def list_days(request: Request, limit: int = 30) -> dict[str, Any]:
    """最近若干天的汇总。新的在前。"""
    limit = max(1, min(limit, 365))
    state = _state(request)
    repo = state.repository

    days = repo.recent_days(limit=limit)

    return {
        "days": [
            {
                "date": key,
                "success": bool((entry or {}).get("success")),
                "attempts": int((entry or {}).get("attempts") or 0),
                "sent_to": list((entry or {}).get("sent_to") or []),
                "first_success_at": (entry or {}).get("first_success_at"),
                "last_run_at": (entry or {}).get("last_run_at"),
                "last_summary": (entry or {}).get("last_summary"),
                "runs": list((entry or {}).get("runs") or []),
            }
            for key, entry in days
        ],
        "today": _today_payload(repo),
        "streak": _streak_payload(repo),
    }


@router.get("/today")
async def today(request: Request) -> dict[str, Any]:
    """今天的状态。前端首屏的横幅用它。"""
    state = _state(request)
    repo = state.repository
    return {
        "today": _today_payload(repo),
        "streak": _streak_payload(repo),
    }


# =============================================================================
# 按次
# =============================================================================


@router.get("")
@router.get("/")
async def list_runs(request: Request, limit: int = 50) -> dict[str, Any]:
    """最近的运行记录列表（精简版，不含每个收件人的明细）。

    摘要来自 ``runs-index.json``（一次读全），不再逐条 ``load_report`` 造成 N+1。
    """
    limit = max(1, min(limit, 500))
    state = _state(request)
    items = state.repository.list_run_summaries(limit=limit)
    return {"runs": items, "count": len(items)}


@router.get("/{run_id}")
async def get_run(request: Request, run_id: str) -> dict[str, Any]:
    """单次运行的完整报告。"""
    state = _state(request)
    report = state.repository.load_report(run_id)

    if report is None:
        raise HTTPException(status_code=404, detail=f"找不到运行记录 {run_id}")

    return _decorate_report(report)


# =============================================================================
# 手动触发
# =============================================================================


class TriggerRequest(BaseModel):
    """手动触发的参数。"""

    dry_run: bool = Field(
        default=False,
        description="true = 只走到「输入完成」不真正发送，用于验证链路",
    )
    kind: str = Field(
        default="manual",
        description="任务来源标签，仅用于工作台「任务进行状态」展示：manual / scheduled",
    )
    prevent_duplicates: bool = Field(
        default=False,
        description=(
            "true = 遵守「今天已发过就跳过」。**手动发送默认 false** —— "
            "用户每点一次按钮就应该真发一次；防重复只服务定时任务。"
        ),
    )
    contacts: list[str] | None = Field(
        default=None,
        description="只给这几个人发。留空表示用配置里启用的全部收件人。",
    )
    async_mode: bool = Field(
        default=True,
        alias="async",
        description="true = 立刻返回并后台执行（推荐）；false = 阻塞到跑完",
    )
    message: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "临时消息内容；给出则本次用它替代文案池（不改保存的配置）。"
            "与 contacts 搭配就是群发页的「勾选的人 + 输入框的内容」。"
        ),
    )

    model_config = {"populate_by_name": True}


@router.post("/now")
def trigger_now(request: Request, payload: TriggerRequest | None = None) -> dict[str, Any]:
    """立刻跑一次。

    这是「每次执行可以自由选人」的入口 —— 传 ``contacts`` 就只发那几个人，
    **不会改动保存的配置**（``run_once`` 的 ``contacts_override`` 参数）。

    默认异步：立刻返回 ``started``，让前端去轮询 ``/api/runs``。
    """
    global _manual_running

    payload = payload or TriggerRequest()
    state = _state(request)
    repo = state.repository

    override: tuple[Contact, ...] | None = None
    if payload.contacts is not None:
        if not payload.contacts:
            raise HTTPException(status_code=422, detail="contacts 不能是空列表；想发全部就别传这个字段")

        known = {c.name: c for c in repo.load_contacts()}
        unknown = [n for n in payload.contacts if n not in known]
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"这些名字不在收件人名单里：{', '.join(unknown)}。请先在「收件人」页添加。",
            )
        override = tuple(known[n] for n in payload.contacts)

    messages_override: tuple[Message, ...] | None = None
    if payload.message is not None:
        text = payload.message.strip()
        if not text:
            raise HTTPException(status_code=422, detail="message 不能是空白")
        messages_override = (Message(kind="text", content=text),)

    # 防连点
    with _manual_lock:
        if _manual_running:
            return {
                "ok": False,
                "started": False,
                "detail": "已经有一次手动任务在跑了，等它结束再点（或者去看运行历史）。",
            }
        _manual_running = True

    try:
        if not payload.async_mode:
            return _run_sync(state, payload, override, messages_override)

        thread = threading.Thread(
            target=_run_background,
            args=(state, payload, override, messages_override),
            name="huohua-manual-run",
            daemon=True,
        )
        thread.start()

        LOGGER.info(
            "手动触发已启动%s：%s",
            "（演练模式）" if payload.dry_run else "",
            "全部收件人" if override is None else f"{len(override)} 个指定收件人",
        )

        return {
            "ok": True,
            "started": True,
            "detail": "任务已开始，去「运行历史」看结果（大约需要几十秒）",
            "dry_run": payload.dry_run,
            "target_count": len(override) if override is not None else None,
            "poll_after_ms": 3000,
        }
    except Exception:
        # 启动线程失败，把标记放回去，否则按钮会永远「占用中」
        with _manual_lock:
            _manual_running = False
        raise


@router.get("/now/status")
async def trigger_status() -> dict[str, Any]:
    """手动任务是否还在跑。前端轮询它来决定按钮能不能点。"""
    with _manual_lock:
        return {"running": _manual_running}


# =============================================================================
# 内部
# =============================================================================


def _run_background(
    state: Any,
    payload: TriggerRequest,
    override: tuple[Contact, ...] | None,
    messages_override: tuple[Message, ...] | None = None,
) -> None:
    """后台线程里跑一次。异常必须吞掉（线程里抛出没人接）。"""
    global _manual_running

    try:
        from ...scheduler import run_once

        report = run_once(
            state.settings,
            repository=state.repository,
            dry_run=payload.dry_run,
            contacts_override=override,
            messages_override=messages_override,
            kind=payload.kind,
            prevent_duplicates=payload.prevent_duplicates,
        )

        if report is None:
            LOGGER.warning("手动任务没有执行（没有启用的收件人/消息，或已有任务在跑）")
        else:
            LOGGER.info("手动任务完成：%s", report.summary())

    except Exception:
        LOGGER.exception("手动任务执行失败")
    finally:
        with _manual_lock:
            _manual_running = False


def _run_sync(
    state: Any,
    payload: TriggerRequest,
    override: tuple[Contact, ...] | None,
    messages_override: tuple[Message, ...] | None = None,
) -> dict[str, Any]:
    """同步跑一次并把结果直接返回。只适合 dry_run 这类很快的场景。"""
    global _manual_running

    try:
        from ...scheduler import run_once

        report = run_once(
            state.settings,
            repository=state.repository,
            dry_run=payload.dry_run,
            contacts_override=override,
            messages_override=messages_override,
            kind=payload.kind,
            prevent_duplicates=payload.prevent_duplicates,
        )
    except Exception as exc:
        LOGGER.exception("手动任务执行失败")
        raise HTTPException(
            status_code=500,
            detail=f"执行失败：{type(exc).__name__}: {exc}",
        ) from exc
    finally:
        with _manual_lock:
            _manual_running = False

    if report is None:
        return {
            "ok": False,
            "started": False,
            "detail": (
                "没有执行 —— 可能是没有启用的收件人、消息池为空，"
                "或者已经有一个任务在跑（同一时刻只允许一个）。"
            ),
        }

    return {
        "ok": report.all_succeeded,
        "started": True,
        "detail": report.summary(),
        "report": _decorate_report(_report_to_dict(report)),
    }


def _report_to_dict(report: Any) -> dict[str, Any]:
    from ...models import report_to_dict

    return report_to_dict(report)


def _decorate_report(report: dict[str, Any]) -> dict[str, Any]:
    """给报告加上前端要的展示字段。

    特别是 ``status_label`` 和 ``failure_hint``：原始 status 是 ``uncertain``
    这种机器词，用户看到需要一句人话解释「这到底算成功还是失败、我该做什么」。
    """
    decorated = dict(report)

    outcomes = []
    for item in report.get("outcomes") or []:
        entry = dict(item)
        entry["status_label"] = _STATUS_LABELS.get(entry.get("status", ""), entry.get("status", ""))
        kind = entry.get("failure_kind")
        entry["failure_kind_label"] = _KIND_LABELS.get(kind, kind) if kind else None
        entry["failure_hint"] = _FAILURE_HINTS.get(kind) if kind else None
        outcomes.append(entry)

    decorated["outcomes"] = outcomes
    decorated["total"] = len(outcomes)
    decorated["succeeded_count"] = sum(1 for o in outcomes if o.get("status") == "success")
    decorated["failed_count"] = sum(1 for o in outcomes if o.get("status") == "failed")
    decorated["uncertain_count"] = sum(1 for o in outcomes if o.get("status") == "uncertain")
    return decorated


_STATUS_LABELS = {
    "success": "成功",
    "failed": "失败",
    "skipped": "已跳过",
    "uncertain": "结果不确定",
}

_KIND_LABELS = {
    "transient": "临时性问题",
    "permanent": "好友或会话不存在",
    "auth": "登录态失效",
    "risk": "疑似风控",
    "config": "配置问题",
    "unknown": "未知原因",
}

_FAILURE_HINTS = {
    "auth": "去「账号」页重新扫码登录，然后手动补发一次。",
    "risk": "今天别再试了。明天换个时间段再发，连续两天都报风控的话停用一周。",
    "permanent": "检查收件人名字是否与抖音里显示的一致；好友改名后需要重新同步。",
    "config": "检查消息配置（图片路径是否存在、表情名是否正确）。",
    "transient": "网络或页面加载的问题，通常重试就能过。可以手动再发一次。",
    "unknown": "去日志里看详细报错；如果反复出现，可能是抖音前端改版了。",
}


def _today_payload(repo: Any) -> dict[str, Any]:
    entry = repo.daily_entry() or {}
    return {
        "date": today_str(),
        "success": bool(entry.get("success")),
        "attempts": int(entry.get("attempts") or 0),
        "sent_to": list(entry.get("sent_to") or []),
        "first_success_at": entry.get("first_success_at"),
        "last_run_at": entry.get("last_run_at"),
        "last_summary": entry.get("last_summary"),
        # 「最近一次运行」的真实结果 —— 首页据此区分
        # 「今天成功过」和「刚才那轮全失败」。缺省 0/False 时前端按老逻辑走。
        "last_ok": bool(entry.get("last_ok", False)),
        "last_total": int(entry.get("last_total") or 0),
        "last_failed": int(entry.get("last_failed") or 0),
        "last_uncertain": int(entry.get("last_uncertain") or 0),
        "runs": list(entry.get("runs") or []),
    }


def _streak_payload(repo: Any) -> dict[str, Any]:
    state = repo.load_streak_state()
    return {
        "consecutive_successes": int(state.get("consecutive_successes") or 0),
        "consecutive_failures": int(state.get("consecutive_failures") or 0),
        "streak_start_date": state.get("streak_start_date"),
        "last_success_date": state.get("last_success_date"),
        "last_run_date": state.get("last_run_date"),
    }
