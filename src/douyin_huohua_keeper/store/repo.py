"""数据仓库 —— 所有落盘读写的唯一入口。

三个领域对象：

==============  ==================================  ==============================
对象             文件                                 说明
==============  ==================================  ==============================
账号状态         ``accounts/<id>.state.json``        Playwright storage_state，等同密码
联系人           ``config/contacts.json``            收件人名单与启用状态
任务配置         ``config/tasks.json``               时间、消息、轮流策略
运行历史         ``runs/<date>.json``                按日聚合，用于算连续天数
                ``runs/<run_id>/report.json``       单次执行详情
连续失败计数     ``config/streak.json``              告警升级的依据
==============  ==================================  ==============================

一条规矩：**不要在别处直接 open 数据文件。** 所有读写都走这里，
这样原子写和锁的约束就只有一处需要维护。
"""

from __future__ import annotations

import base64
import contextlib
import functools
import hashlib
import logging
import re
import secrets
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..models import Contact, Message, RunReport, RunStatus, Schedule, SendInterval
from .atomic import atomic_write_json, cleanup_stale_temp_files, read_json
from .lock import FileLock

LOGGER = logging.getLogger(__name__)

# 数据格式版本。将来结构变了靠它做迁移。
SCHEMA_VERSION = 1

# 合法的 ID：字母、数字、下划线、连字符，1~64 位。
#
# 账号 ID 和 run_id 都会被**拼进文件名**（``accounts/<id>.state.json``、
# ``runs/<run_id>/report.json``），而它们可能直接来自 HTTP 请求。
# 不校验的话 ``../../x`` 就能越出数据目录 —— 删/读任意文件。
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def safe_id(value: str, *, kind: str = "ID") -> str:
    """校验「会被当作文件名用的标识符」，挡住路径穿越。不合法就抛 ``ValueError``。

    这是**最后一道防线**：所有拼路径的地方都走它。调用方（路由）最好在更早
    的地方也校验一次，这样能给用户一个 400 而不是 500。
    """
    text = str(value or "").strip()
    if not _SAFE_ID.fullmatch(text):
        raise ValueError(f"{kind} 不合法：{value!r} —— 只允许字母、数字、下划线、连字符，长度 1~64")
    return text


# 进程内串行化：工作台（网页）与定时调度器跑在**同一个进程**里，
# 两边都会对 contacts / streak / runs-index 做「读-改-写」。
# 没有这把锁，并发时后写的会覆盖先写的（实测会丢「今天已发」标记）。
# ⚠ 只保护**本进程**；cron --run-once 是另一个进程，那边靠 run_lock 互斥。
_REPO_LOCK = threading.RLock()


def _serialized(fn):
    """把 Repository 的「读-改-写」方法串行化（可重入）。"""

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with _REPO_LOCK:
            return fn(self, *args, **kwargs)

    return wrapper


def today_str() -> str:
    """本地日期，YYYY-MM-DD。火花按自然日算，所以用本地时区而不是 UTC。"""
    return date.today().isoformat()


def now_str() -> str:
    """本地时间戳，秒级。"""
    return datetime.now().isoformat(timespec="seconds")


def new_run_id() -> str:
    """生成运行 ID。时间前缀便于排序，随机后缀避免同秒冲突。"""
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


class Repository:
    """数据读写门面。

    实例本身是轻量的（只持有路径），可以放心到处传递。
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.accounts_dir = self.data_dir / "accounts"
        self.config_dir = self.data_dir / "config"
        self.runs_dir = self.data_dir / "runs"

    def ensure_layout(self) -> None:
        for directory in (self.accounts_dir, self.config_dir, self.runs_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def cleanup(self) -> int:
        """清理残留临时文件和过期锁。启动时调一次。

        只清「够老」的临时文件（默认 1 小时）—— 刚生成的可能是另一个进程
        正在写入的，误删会导致那次写入失败。
        """
        return cleanup_stale_temp_files(self.data_dir)

    # =========================================================================
    # 全局运行锁
    # =========================================================================

    @property
    def run_lock_path(self) -> Path:
        return self.data_dir / ".run.lock"

    def run_lock(self) -> FileLock:
        """获取全局运行锁。一次只允许一个发送任务在跑。"""
        return FileLock(self.run_lock_path)

    # =========================================================================
    # 账号状态
    # =========================================================================

    def account_state_path(self, account_id: str) -> Path:
        return self.accounts_dir / f"{safe_id(account_id, kind='账号 ID')}.state.json"

    def save_account_state(self, account_id: str, state: dict[str, Any]) -> Path:
        """保存 Playwright storage_state。

        ⚠️ 这个文件等同于账号密码。写完之后立刻收紧权限：
        在 POSIX 上设成 600，Windows 上依赖用户目录的 ACL。
        """
        path = self.account_state_path(account_id)
        atomic_write_json(path, state)
        _restrict_permissions(path)
        return path

    def load_account_state(self, account_id: str) -> dict[str, Any] | None:
        path = self.account_state_path(account_id)
        return read_json(path, default=None)

    def account_state_age_days(self, account_id: str) -> float | None:
        """登录态多久没刷新了。用于判断「可能快过期了」。"""
        path = self.account_state_path(account_id)
        if not path.exists():
            return None
        import time

        return (time.time() - path.stat().st_mtime) / 86400

    def has_account_state(self, account_id: str) -> bool:
        return self.account_state_path(account_id).exists()

    def delete_account_state(self, account_id: str) -> bool:
        path = self.account_state_path(account_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def list_accounts(self) -> tuple[str, ...]:
        """列出所有已有登录态的账号 ID。"""
        if not self.accounts_dir.is_dir():
            return ()
        return tuple(sorted(p.name[: -len(".state.json")] for p in self.accounts_dir.glob("*.state.json")))

    # =========================================================================
    # 联系人
    # =========================================================================

    @property
    def contacts_path(self) -> Path:
        return self.config_dir / "contacts.json"

    def load_contacts(self) -> tuple[Contact, ...]:
        payload = read_json(self.contacts_path, default=None)
        if not payload:
            return ()
        raw_items = payload.get("contacts") if isinstance(payload, dict) else payload
        if not isinstance(raw_items, list):
            return ()
        return tuple(_contact_from_dict(item) for item in raw_items if isinstance(item, dict))

    def save_contacts(self, contacts: tuple[Contact, ...]) -> None:
        atomic_write_json(
            self.contacts_path,
            {
                "version": SCHEMA_VERSION,
                "updated_at": now_str(),
                "contacts": [_contact_to_payload(c) for c in contacts],
            },
        )

    def enabled_contacts(self) -> tuple[Contact, ...]:
        return tuple(c for c in self.load_contacts() if c.enabled)

    @_serialized
    def upsert_contact(self, contact: Contact) -> tuple[Contact, ...]:
        """插入或更新一个联系人（按 name 匹配），返回更新后的全量名单。"""
        existing = list(self.load_contacts())
        for index, item in enumerate(existing):
            if item.name == contact.name:
                existing[index] = contact
                break
        else:
            existing.append(contact)
        updated = tuple(existing)
        self.save_contacts(updated)
        return updated

    @_serialized
    def remove_contact(self, name: str) -> tuple[Contact, ...]:
        updated = tuple(c for c in self.load_contacts() if c.name != name)
        self.save_contacts(updated)
        return updated

    @_serialized
    def mark_sent_today(self, names: tuple[str, ...], on: str | None = None) -> None:
        """记录今天已经给这些人发过了。防重复的第一道依据。"""
        stamp = on or today_str()
        updated = tuple(
            Contact(
                name=c.name,
                conversation_id=c.conversation_id,
                is_group=c.is_group,
                note=c.note,
                enabled=c.enabled,
                weight=c.weight,
                last_sent_on=stamp,
            )
            if c.name in names
            else c
            for c in self.load_contacts()
        )
        self.save_contacts(updated)

    # =========================================================================
    # 好友档案缓存（火花 / 头像）
    #
    # ⚠️ 头像**不存进 friends.json**。早期版本把头像以 base64 内嵌，
    # 结果 friends.json 随好友数线性膨胀（实测到 1MB+），且每次列表接口都要
    # 全量读+解析这个大文件。现在头像落独立文件（avatar_dir），friends.json
    # 只留 {"streak", "avatar_ext"}，列表响应也只回 has_avatar 标记，
    # 真正要头像时走按需接口 /api/contacts/avatar?name=。
    # =========================================================================

    @property
    def friend_profiles_path(self) -> Path:
        return self.config_dir / "friends.json"

    @property
    def avatar_dir(self) -> Path:
        return self.config_dir / "avatars"

    @staticmethod
    def _avatar_key(name: str) -> str:
        """把任意（可能含中文/特殊字符的）名字映射成安全的文件名。

        不直接用名字当文件名：中文名会被 safe_id 拒掉，且裸名字有路径穿越风险。
        用 sha1 前缀则既安全又稳定（同名字永远落到同一文件）。
        """
        return hashlib.sha1(name.encode("utf-8")).hexdigest()[:32]

    def load_friend_profiles(self) -> dict[str, dict[str, Any]]:
        """读取好友档案缓存：``{名字: {"streak": ..., "avatar_ext": ...}}``。

        读失败一律退化成空字典 —— 缓存坏了顶多没头像，不该让页面挂掉。
        """
        payload = read_json(self.friend_profiles_path, default=None)
        if not isinstance(payload, dict):
            return {}
        profiles = payload.get("profiles")
        if not isinstance(profiles, dict):
            return {}
        return {k: v for k, v in profiles.items() if isinstance(v, dict)}

    def save_friend_avatar(self, name: str, data: bytes, ext: str) -> None:
        """把头像字节落盘到 ``avatar_dir/<sha1(name)>.<ext>``。"""
        ext = (ext or "bin").lower()
        if ext not in {"png", "jpg", "jpeg", "webp", "gif", "bin"}:
            ext = "bin"
        self.avatar_dir.mkdir(parents=True, exist_ok=True)
        key = self._avatar_key(name)
        path = self.avatar_dir / f"{key}.{ext}"
        path.write_bytes(data)
        # 同一个名字换格式时清掉旧扩展名的残留，避免垃圾文件越积越多
        for old in self.avatar_dir.glob(f"{key}.*"):
            if old != path:
                with contextlib.suppress(OSError):
                    old.unlink()

    def load_friend_avatar(self, name: str) -> tuple[bytes, str] | None:
        """读头像字节与 MIME。优先读文件；兼容旧版仍内嵌 base64 的档案。"""
        entry = self.load_friend_profiles().get(name) or {}
        ext = entry.get("avatar_ext")
        if ext:
            path = self.avatar_dir / f"{self._avatar_key(name)}.{ext}"
            if path.is_file():
                mime = {
                    "png": "image/png",
                    "jpg": "image/jpeg",
                    "jpeg": "image/jpeg",
                    "webp": "image/webp",
                    "gif": "image/gif",
                }.get(ext, "application/octet-stream")
                return path.read_bytes(), mime
        # 兼容旧版：头像仍以 base64 data URL 存在 friends.json（首次重同步后会被清理）
        data_url = entry.get("avatar_data")
        if isinstance(data_url, str) and data_url.startswith("data:"):
            try:
                ext2, data = _decode_data_url(data_url)
            except Exception:  # noqa: BLE001
                return None
            mime = {
                "png": "image/png",
                "jpg": "image/jpeg",
                "webp": "image/webp",
                "gif": "image/gif",
            }.get(ext2, "application/octet-stream")
            return data, mime
        return None

    def has_friend_avatar(self, name: str) -> bool:
        entry = self.load_friend_profiles().get(name) or {}
        if entry.get("avatar_ext"):
            return (self.avatar_dir / f"{self._avatar_key(name)}.{entry['avatar_ext']}").is_file()
        return False

    def save_friend_profiles(self, profiles: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """把新读到的好友档案**合并**进缓存（按名字）。

        合并而不是覆盖：一次同步往往只读到最近有聊天的会话，
        直接覆盖会把其他好友已有的头像 / 火花抹掉。

        头像统一落文件：传入 ``avatar_data``（data URL）会解码存盘并记
        ``avatar_ext``；传入 ``avatar_ext`` 则原样保留。无论哪种，friends.json
        里都不会再留 base64。
        """
        merged = self.load_friend_profiles()
        for name, profile in profiles.items():
            if not name:
                continue
            entry = dict(merged.get(name) or {})
            entry.update(profile)
            # 头像一律存文件，friends.json 不再留 base64（避免随好友数膨胀）
            entry.pop("avatar_data", None)
            incoming = profile.get("avatar_data")
            if isinstance(incoming, str) and incoming.startswith("data:"):
                try:
                    ext, data = _decode_data_url(incoming)
                    self.save_friend_avatar(name, data, ext)
                    entry["avatar_ext"] = ext
                except Exception:  # noqa: BLE001
                    LOGGER.debug("头像 data URL 解析失败，跳过：%s", name)
            elif profile.get("avatar_ext"):
                entry["avatar_ext"] = profile["avatar_ext"]
            entry["updated_at"] = now_str()
            merged[name] = entry
        atomic_write_json(
            self.friend_profiles_path,
            {"version": SCHEMA_VERSION, "updated_at": now_str(), "profiles": merged},
        )
        return merged

    # =========================================================================
    # 任务配置
    # =========================================================================

    @property
    def tasks_path(self) -> Path:
        return self.config_dir / "tasks.json"

    def load_tasks(self) -> dict[str, Any]:
        """读取任务配置。

        返回原始字典而不是 dataclass —— 任务配置会随版本演进，
        先保持灵活，等结构稳定了再收紧类型。
        """
        payload = read_json(self.tasks_path, default=None)
        if not payload:
            return _default_tasks()
        if not isinstance(payload, dict):
            return _default_tasks()
        payload.setdefault("version", SCHEMA_VERSION)
        return payload

    def save_tasks(self, tasks: dict[str, Any]) -> None:
        payload = dict(tasks)
        payload["version"] = SCHEMA_VERSION
        payload["updated_at"] = now_str()
        atomic_write_json(self.tasks_path, payload)

    def load_schedule(self) -> Schedule:
        """从任务配置里抽出调度设置。"""
        tasks = self.load_tasks()
        raw = tasks.get("schedule") or {}
        return Schedule(
            enabled=bool(raw.get("enabled", True)),
            hour=int(raw.get("hour", 10)),
            minute=int(raw.get("minute", 30)),
            jitter_minutes=int(raw.get("jitter_minutes", 25)),
            timezone=str(raw.get("timezone", "Asia/Shanghai")),
        )

    def save_schedule(self, schedule: Schedule) -> None:
        tasks = self.load_tasks()
        tasks["schedule"] = {
            "enabled": schedule.enabled,
            "hour": schedule.hour,
            "minute": schedule.minute,
            "jitter_minutes": schedule.jitter_minutes,
            "timezone": schedule.timezone,
        }
        self.save_tasks(tasks)

    def load_messages(self) -> tuple[Message, ...]:
        """从任务配置里抽出消息候选池。"""
        tasks = self.load_tasks()
        raw_items = tasks.get("messages") or []
        messages: list[Message] = []
        for item in raw_items:
            try:
                messages.append(_message_from_dict(item))
            except (ValueError, KeyError, TypeError):
                # 单条消息配置有问题不该让整个任务起不来 ——
                # 但它会被跳过，而且这在配置校验接口里会暴露出来
                continue
        return tuple(messages)

    def load_interval(self) -> SendInterval:
        tasks = self.load_tasks()
        raw = tasks.get("interval") or {}
        return SendInterval(
            minimum=float(raw.get("minimum", 3.0)),
            maximum=float(raw.get("maximum", 8.0)),
        )

    def load_task_bundle(self) -> dict[str, Any]:
        """一次读取任务配置，结构化返回 schedule / interval / messages / rotation。

        为什么要这个：``get_tasks``、``preview``、``overview`` 各自又去调
        ``load_schedule`` / ``load_messages`` / ``load_interval``，而它们**每个**
        都会重新解析一遍 ``tasks.json`` —— 一个请求里最多解析 5 次。
        这个方法只解析一次，把需要的部分一次给全。
        """
        tasks = self.load_tasks()
        raw_schedule = tasks.get("schedule") or {}
        raw_interval = tasks.get("interval") or {}

        messages: list[Message] = []
        for item in tasks.get("messages") or []:
            try:
                messages.append(_message_from_dict(item))
            except (ValueError, KeyError, TypeError):
                # 单条消息配置有问题不该让整个任务起不来 —— 跳过，配置校验接口会暴露
                continue

        return {
            "schedule": Schedule(
                enabled=bool(raw_schedule.get("enabled", True)),
                hour=int(raw_schedule.get("hour", 10)),
                minute=int(raw_schedule.get("minute", 30)),
                jitter_minutes=int(raw_schedule.get("jitter_minutes", 25)),
                timezone=str(raw_schedule.get("timezone", "Asia/Shanghai")),
            ),
            "interval": SendInterval(
                minimum=float(raw_interval.get("minimum", 3.0)),
                maximum=float(raw_interval.get("maximum", 8.0)),
            ),
            "messages": tuple(messages),
            "rotation": str(tasks.get("rotation") or "all"),
            "updated_at": tasks.get("updated_at"),
            "raw": tasks,
        }

    # =========================================================================
    # 连续失败计数
    # =========================================================================

    @property
    def streak_path(self) -> Path:
        return self.config_dir / "streak.json"

    def load_streak_state(self) -> dict[str, Any]:
        """连续失败 / 连续成功的计数状态。"""
        payload = read_json(self.streak_path, default=None)
        if not isinstance(payload, dict):
            return {
                "consecutive_failures": 0,
                "consecutive_successes": 0,
                "last_run_date": None,
                "last_success_date": None,
                "last_alert_level": None,
                "streak_start_date": None,
            }
        payload.setdefault("consecutive_failures", 0)
        payload.setdefault("consecutive_successes", 0)
        return payload

    def save_streak_state(self, state: dict[str, Any]) -> None:
        payload = dict(state)
        payload["updated_at"] = now_str()
        atomic_write_json(self.streak_path, payload)

    @_serialized
    def record_success(self) -> dict[str, Any]:
        """记一次成功：连续失败归零，连续成功加一。"""
        state = self.load_streak_state()
        today = today_str()

        # 同一天重复成功不重复累加连续天数
        if state.get("last_success_date") != today:
            state["consecutive_successes"] = int(state.get("consecutive_successes") or 0) + 1
        state["consecutive_failures"] = 0
        state["last_success_date"] = today
        state["last_run_date"] = today
        state["last_alert_level"] = None

        if not state.get("streak_start_date"):
            state["streak_start_date"] = today

        self.save_streak_state(state)
        return state

    @_serialized
    def record_failure(self) -> dict[str, Any]:
        """记一次失败：连续成功归零，连续失败加一。"""
        state = self.load_streak_state()
        today = today_str()

        # 同一天重复失败不重复累加连续天数（否则手动重试三次就变 CRITICAL 了）
        if state.get("last_run_date") != today:
            state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
        state["consecutive_successes"] = 0
        state["last_run_date"] = today

        self.save_streak_state(state)
        return state

    # =========================================================================
    # 运行历史
    # =========================================================================

    def run_dir(self, run_id: str) -> Path:
        directory = self.runs_dir / safe_id(run_id, kind="run ID")
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def save_report(self, report: RunReport) -> Path:
        """保存单次运行报告，并同步更新当天的日汇总。"""
        from ..models import report_to_dict

        directory = self.run_dir(report.run_id)
        path = directory / "report.json"
        atomic_write_json(path, report_to_dict(report))
        self._append_to_daily(report)
        self._append_to_runs_index(report)
        return path

    def load_report(self, run_id: str) -> dict[str, Any] | None:
        # 读路径上遇到非法 ID 就当「没有这份记录」—— route 会回 404。
        # 这里不该抛异常（那会变成 500），但也绝不能拿它去拼路径。
        try:
            safe = safe_id(run_id, kind="run ID")
        except ValueError as exc:
            LOGGER.warning("拒绝读取非法 run_id：%r（%s）", run_id, exc)
            return None
        return read_json(self.runs_dir / safe / "report.json", default=None)

    @property
    def daily_index_path(self) -> Path:
        return self.runs_dir / "index.json"

    @property
    def run_index_path(self) -> Path:
        return self.runs_dir / "runs-index.json"

    @_serialized
    def _append_to_daily(self, report: RunReport) -> None:
        """维护按日聚合的索引。

        为什么要有这个：判断「今天是否成功过」不需要读所有详细报告，
        读一个索引就够了。文件会随天数增长，但每天一条记录，跑十年也就几千条。
        """
        payload = read_json(self.daily_index_path, default=None)
        if not isinstance(payload, dict):
            payload = {"version": SCHEMA_VERSION, "days": {}}

        days = payload.setdefault("days", {})
        day_key = (report.finished_at or now_str())[:10]

        entry = days.get(day_key) or {"runs": [], "success": False, "attempts": 0}
        entry["runs"] = [*entry.get("runs", []), report.run_id][-50:]  # 只留最近 50 条
        entry["attempts"] = int(entry.get("attempts", 0)) + 1

        succeeded = report.all_succeeded or bool(report.succeeded)
        entry["success"] = bool(entry.get("success")) or succeeded
        if succeeded:
            entry["first_success_at"] = entry.get("first_success_at") or report.finished_at
            entry["sent_to"] = sorted({o.name for o in report.succeeded})
        entry["last_run_at"] = report.finished_at
        entry["last_summary"] = report.summary()

        # ⚠️ ``success`` 是**按天「或」累积**的：今天只要成功过一次就永远是 True。
        # 于是会出现一个很危险的显示：早上续上了火花，晚上那次 15/15 全失败，
        # 首页却仍然是一句绿色的「今天的火花已经续上了」（实测踩过）。
        #
        # 所以额外记录「最近一次运行」的真实结果，让首页能区分
        # 「整体成功过」和「刚才那一轮其实全挂了」。
        entry["last_ok"] = bool(succeeded)
        entry["last_total"] = len(report.outcomes)
        entry["last_failed"] = len(report.failed)
        entry["last_uncertain"] = sum(1 for o in report.outcomes if o.status is RunStatus.UNCERTAIN)

        days[day_key] = entry
        payload["updated_at"] = now_str()

        atomic_write_json(self.daily_index_path, payload)

    def today_succeeded(self, on: str | None = None) -> bool:
        """今天是否已经成功发送过。供 CLI ``--check-today`` 使用。"""
        day_key = on or today_str()
        payload = read_json(self.daily_index_path, default=None)
        if not isinstance(payload, dict):
            return False
        entry = (payload.get("days") or {}).get(day_key)
        if not isinstance(entry, dict):
            return False
        return bool(entry.get("success"))

    def daily_entry(self, on: str | None = None) -> dict[str, Any] | None:
        day_key = on or today_str()
        payload = read_json(self.daily_index_path, default=None)
        if not isinstance(payload, dict):
            return None
        entry = (payload.get("days") or {}).get(day_key)
        return entry if isinstance(entry, dict) else None

    def recent_days(self, limit: int = 30) -> list[tuple[str, dict[str, Any]]]:
        """最近的日记录，新的在前。工作台的历史页面用它。"""
        payload = read_json(self.daily_index_path, default=None)
        if not isinstance(payload, dict):
            return []
        days = payload.get("days") or {}
        items = sorted(days.items(), key=lambda kv: kv[0], reverse=True)
        return [(k, v) for k, v in items[:limit] if isinstance(v, dict)]

    def list_run_ids(self, limit: int = 50) -> list[str]:
        if not self.runs_dir.is_dir():
            return []
        dirs = [p for p in self.runs_dir.iterdir() if p.is_dir() and (p / "report.json").exists()]
        return sorted((p.name for p in dirs), reverse=True)[:limit]

    @_serialized
    def _append_to_runs_index(self, report: RunReport) -> None:
        """维护「每次运行」的轻量摘要索引。

        ``/api/runs``（按次看历史）原本要逐条 ``load_report`` 全量读（N+1），
        几百条运行就是几百次文件读。这里在每次保存报告时把摘要写进
        ``runs-index.json``，列表页一次读全，开销从 O(N 次读) 降到 O(1)。
        """
        from ..models import report_to_dict

        data = report_to_dict(report) if not isinstance(report, dict) else report
        summary = _run_summary_from_dict(data)

        payload = read_json(self.run_index_path, default=None)
        if not isinstance(payload, dict):
            payload = {"version": SCHEMA_VERSION, "runs": {}}
        runs = payload.setdefault("runs", {})
        runs[summary["run_id"]] = summary
        payload["updated_at"] = now_str()
        atomic_write_json(self.run_index_path, payload)

    def list_run_summaries(self, limit: int = 50) -> list[dict[str, Any]]:
        """最近的运行摘要（不含逐条明细），新的在前。供 ``/api/runs`` 列表用。

        优先读 ``runs-index.json``（一次读全）；没有索引的旧部署退回逐文件读，
        并顺便把缺失的摘要补写进索引（自愈），之后就走快路径了。
        """
        limit = max(1, min(limit, 500))
        payload = read_json(self.run_index_path, default=None)
        index = payload.get("runs") if isinstance(payload, dict) else None

        if not index:
            return self._list_run_summaries_legacy(limit)

        known = set(index.keys())
        missing = [rid for rid in self.list_run_ids(limit=limit) if rid not in known]
        summaries = list(index.values())
        if missing:
            for rid in missing[:200]:
                rep = self.load_report(rid)
                if isinstance(rep, dict):
                    summaries.append(_run_summary_from_dict(rep))
                    index[rid] = summaries[-1]
            # 自愈：把这次补齐的写回索引，下次就走快路径
            payload["runs"] = index
            payload["updated_at"] = now_str()
            atomic_write_json(self.run_index_path, payload)

        summaries.sort(
            key=lambda s: s.get("finished_at") or s.get("started_at") or "",
            reverse=True,
        )
        return summaries[:limit]

    def _list_run_summaries_legacy(self, limit: int) -> list[dict[str, Any]]:
        items = []
        for run_id in self.list_run_ids(limit=limit):
            report = self.load_report(run_id)
            if isinstance(report, dict):
                items.append(_run_summary_from_dict(report))
        items.sort(
            key=lambda s: s.get("finished_at") or s.get("started_at") or "",
            reverse=True,
        )
        return items[:limit]

    # =========================================================================
    # 内部：配置文件损坏时的现场保护
    # =========================================================================


# =============================================================================
# 转换辅助
# =============================================================================


def _contact_from_dict(payload: dict[str, Any]) -> Contact:
    return Contact(
        name=str(payload.get("name") or payload.get("remark") or "").strip(),
        conversation_id=payload.get("conversation_id"),
        is_group=bool(payload.get("is_group", False)),
        note=payload.get("note"),
        enabled=bool(payload.get("enabled", True)),
        weight=int(payload.get("weight", 1)),
        last_sent_on=payload.get("last_sent_on"),
    )


def _contact_to_payload(contact: Contact) -> dict[str, Any]:
    return {
        "name": contact.name,
        "conversation_id": contact.conversation_id,
        "is_group": contact.is_group,
        "note": contact.note,
        "enabled": contact.enabled,
        "weight": contact.weight,
        "last_sent_on": contact.last_sent_on,
    }


def _message_from_dict(payload: Any) -> Message:
    if isinstance(payload, str):
        # 简写形式：消息池里直接写字符串就当文本消息
        return Message(kind="text", content=payload)

    if not isinstance(payload, dict):
        raise TypeError(f"消息配置必须是字符串或对象，收到 {type(payload).__name__}")

    kind = payload.get("kind") or payload.get("type") or "text"
    if kind == "random":
        choices = tuple(_message_from_dict(item) for item in (payload.get("choices") or []))
        return Message(kind="random", choices=choices)

    path = payload.get("path")
    return Message(
        kind=kind,
        content=payload.get("content") or payload.get("text"),
        path=Path(path) if path else None,
        sticker=payload.get("sticker"),
    )


def _decode_data_url(data_url: str) -> tuple[str, bytes]:
    """解析 ``data:image/png;base64,xxxx`` 形式，返回 (扩展名, 字节)。"""
    meta, _, b64 = data_url.partition(",")
    if not b64:
        raise ValueError("不是合法的 data URL")
    mime = meta.split(";")[0].replace("data:", "").strip() or "image/jpeg"
    ext = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/webp": "webp",
        "image/gif": "gif",
    }.get(mime, "bin")
    return ext, base64.b64decode(b64)


def _run_summary_from_dict(report: dict[str, Any]) -> dict[str, Any]:
    """从单次运行报告（dict）里抠出列表页要的摘要字段。

    原本 ``/api/runs`` 是每条报告都 ``load_report`` 全量读一次（N+1），
    现在摘要统一存进 ``runs-index.json``，列表页一次读全。
    """
    outcomes = report.get("outcomes") or []
    return {
        "run_id": report.get("run_id"),
        "started_at": report.get("started_at"),
        "finished_at": report.get("finished_at"),
        "dry_run": bool(report.get("dry_run")),
        "summary": report.get("summary"),
        "consecutive_failures": report.get("consecutive_failures", 0),
        "total": len(outcomes),
        "succeeded": sum(1 for o in outcomes if o.get("status") == "success"),
        "failed": sum(1 for o in outcomes if o.get("status") == "failed"),
        "uncertain": sum(1 for o in outcomes if o.get("status") == "uncertain"),
        "skipped": sum(1 for o in outcomes if o.get("status") == "skipped"),
    }


def _default_tasks() -> dict[str, Any]:
    """首次启动的默认任务配置。

    默认 10:30 发送、25 分钟抖动：早上比深夜更像人在聊天，
    而抖动让每日发送时间不会整齐到一眼可辨。
    """
    return {
        "version": SCHEMA_VERSION,
        "schedule": {
            "enabled": True,
            "hour": 10,
            "minute": 30,
            "jitter_minutes": 25,
            "timezone": "Asia/Shanghai",
        },
        "interval": {"minimum": 3.0, "maximum": 8.0},
        "messages": [],
        "rotation": "all",  # all / round_robin / random
    }


def _restrict_permissions(path: Path) -> None:
    """把文件权限收紧到 600。Windows 上跳过（依赖用户目录 ACL）。"""
    import os

    if os.name == "nt":
        return
    with contextlib.suppress(OSError):
        path.chmod(0o600)


# =============================================================================
# 便捷函数（供 CLI 直接调用）
# =============================================================================


def repository_for(settings) -> Repository:
    return Repository(settings.data_dir)


def today_succeeded() -> bool:
    """模块级便捷函数，CLI 的 ``--check-today`` 用。"""
    from ..config import load_settings

    settings = load_settings()
    return Repository(settings.data_dir).today_succeeded()
