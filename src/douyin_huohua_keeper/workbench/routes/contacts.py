"""收件人管理。

三件事：从抖音同步会话列表、增删改收件人、试发一条。

**关于「同步会话列表」**：它会打开浏览器读一遍会话列表，是个有副作用的
操作（会导航页面）。因此它不会自动跑 —— 必须用户点一下才执行。
这和「定时发送」共享同一个浏览器引擎，所以两个操作会串行，不会打架。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from ...models import Contact
from ...store.repo import today_str

LOGGER = logging.getLogger(__name__)

router = APIRouter(prefix="/api/contacts", tags=["contacts"])

# 收件人列表每页多少个。15 是「一屏基本放得下、又不用滚很长」的折中。
DEFAULT_PAGE_SIZE = 15
MAX_PAGE_SIZE = 200


def _state(request: Request) -> Any:
    return request.app.state.keeper


# =============================================================================
# 读
# =============================================================================


@router.get("")
@router.get("/")
async def list_contacts(
    request: Request,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    """分页的收件人名单，附带「今天是否已发」标记与好友档案（火花 / 头像）。

    为什么要分页：一次同步可能进来上百个会话，一次全渲染又慢又长。
    只把**当前这一页**的联系人（以及他们的头像）发给前端，翻页才取下一页。

    排序：**已启用优先**，其余保持原有顺序 —— 打开开关的人自动浮到最前，
    不用往下翻着找。
    """
    return _contacts_page(_state(request), page=page, page_size=page_size)


# =============================================================================
# 同步会话列表
# =============================================================================


class SyncRequest(BaseModel):
    """同步选项。"""

    limit: int = Field(default=300, ge=1, le=500, description="最多读取多少条会话")
    merge: bool = Field(
        default=True,
        description=(
            "true = 合并进现有名单（不会删掉你手工加的人）；"
            "false = 用会话列表覆盖（**会删掉不在列表里的人**）"
        ),
    )


@router.post("/sync")
def sync_contacts(request: Request, payload: SyncRequest | None = None) -> dict[str, Any]:
    """从抖音会话列表同步名字。

    默认是**合并**而不是覆盖 —— 覆盖会静默删掉用户手工加的人，
    而用户点「同步」的本意多半是「把新好友加进来」。
    真要覆盖得显式传 ``merge=false``。
    """
    payload = payload or SyncRequest()
    state = _state(request)
    repo = state.repository

    from ...engine import navigator

    try:
        engine = state.get_engine()
        # 工作台引擎是随用随启的新浏览器，**不会自动带登录态** ——
        # 不装载就会停在登录页，读到的会话列表恒为空。
        # （回归：点「从抖音同步」永远「没有读到任何会话」，就是漏了这两步。）
        _ensure_logged_in(state)
        check = engine.check_login()
        if not check.ok:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"打开会话页失败：{check.detail}",
            )
        # 读**富信息**（名字 + 火花 + 头像）：收件人列表要展示头像和火花天数，
        # 顺手把头像下载落到文件（不再内嵌 base64）。
        details = engine.session.call(
            lambda ctx: navigator.read_conversation_details(ctx.page, limit=payload.limit)
        )
        details = engine.session.call(lambda ctx: _embed_avatars(ctx.page, details, repo))
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.error("同步会话列表失败：%s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"读取会话列表失败：{type(exc).__name__}: {exc}\n"
                "常见原因：登录态失效（去账号页重新扫码）、Chromium 未安装、"
                "或网络访问不了抖音。"
            ),
        ) from exc

    # 缓存好友档案（火花 / 头像引用），供「收件人」页展示
    profiles = {
        d["name"]: {"streak": d.get("streak") or "", "avatar_ext": d.get("avatar_ext")}
        for d in details
        if d.get("name")
    }
    if profiles:
        repo.save_friend_profiles(profiles)

    names = tuple(dict.fromkeys(d["name"] for d in details if d.get("name")))  # 去重且保持顺序
    if not names:
        return {
            "ok": False,
            "synced": 0,
            "detail": (
                "没有读到任何会话。会话列表默认只显示近期有聊天的对象 —— "
                "如果列表确实是空的，或者页面结构变了，都可能出现这个结果。"
            ),
            **_contacts_page(state),
        }

    existing = {c.name: c for c in repo.load_contacts()}
    groups = {d["name"]: bool(d.get("is_group")) for d in details if d.get("name")}

    if payload.merge:
        added: list[str] = []
        merged = list(existing.values())
        for name in names:
            if name not in existing:
                # 新增的**默认停用**（与「群发页」一致）。
                # 同步的本意是「把好友纳进来备用」；若默认启用，一次同步就会让
                # 每日定时任务给所有好友群发 —— 这是个危险且难察觉的副作用。
                # 要发给谁，在列表里单独打开即可。
                merged.append(
                    Contact(name=name, enabled=False, is_group=groups.get(name, False))
                )
                added.append(name)
        if added:
            # **一次性落盘**。逐个 upsert 会反复「读全量 + 写全量」，
            # 一次同步上百人时是 O(n²) 的 IO（实测踩过）。
            repo.save_contacts(tuple(merged))
        detail = f"新增 {len(added)} 个（默认停用，需手动启用），已有 {len(names) - len(added)} 个"
    else:
        old_names = set(existing)
        keep = tuple(
            Contact(
                name=name,
                conversation_id=existing[name].conversation_id if name in existing else None,
                is_group=existing[name].is_group if name in existing else groups.get(name, False),
                note=existing[name].note if name in existing else None,
                # 覆盖模式下，新名字同样默认停用（理由同上）
                enabled=existing[name].enabled if name in existing else False,
                weight=existing[name].weight if name in existing else 1,
                last_sent_on=existing[name].last_sent_on if name in existing else None,
            )
            for name in names
        )
        repo.save_contacts(keep)
        removed = sorted(old_names - set(names))
        added = [n for n in names if n not in old_names]
        detail = f"已覆盖：保留 {len(keep)} 个"
        if added:
            detail += f"，新增 {len(added)} 个"
        if removed:
            detail += f"，移除 {len(removed)} 个（{', '.join(removed[:5])}）"

    return {
        "ok": True,
        "synced": len(names),
        "detail": detail,
        "names": list(names),
        **_contacts_page(state),
    }


@router.get("/discovered")
def discovered(request: Request) -> dict[str, Any]:
    """只读地看一眼会话列表里都有谁，不写任何配置。

    比 ``sync`` 安全 —— 想先看看再决定要不要加的时候用它。
    """
    state = _state(request)

    from ...engine import navigator

    try:
        engine = state.get_engine()
        # 同 sync：先把已保存的登录态装进这个新浏览器，否则只会读到登录页
        _ensure_logged_in(state)
        check = engine.check_login()
        if not check.ok:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"打开会话页失败：{check.detail}",
            )
        names = engine.session.call(lambda ctx: navigator.list_contact_names(ctx.page, limit=300))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"读取会话列表失败：{type(exc).__name__}: {exc}",
        ) from exc

    configured = {c.name for c in state.repository.load_contacts()}
    return {
        "names": list(names),
        "new_names": [n for n in names if n not in configured],
        "count": len(names),
    }


@router.get("/friends")
def list_friends(request: Request, limit: int = 300) -> dict[str, Any]:
    """好友列表（富信息）：名字 + 火花状态 + 头像。

    给「群发页」用 —— 那里要展示和手机抖音一致的
    火焰 🔥 与连续天数，并带真实头像。

    慢操作（打开浏览器 + 导航 + 逐个下载头像，约 15~40 秒），
    前端必须给加载提示。头像用**已登录浏览器上下文**下载
    （带 Cookie/Referer，绕过 CDN 防盗链），转 base64 内嵌返回。
    """
    state = _state(request)

    from ...engine import navigator

    try:
        engine = state.get_engine()
        # 工作台的引擎是随用随启的新浏览器，**不会自动带登录态** ——
        # 不装载的话看到的全是登录页（实测踩过）。已保存的登录态存在就主动加载。
        _ensure_logged_in(state)
        # 导航到会话页（check_login 内部会 goto + 轮询等待渲染）
        check = engine.check_login()
        if not check.ok:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"打开会话页失败：{check.detail}",
            )

        details = engine.session.call(
            lambda ctx: navigator.read_conversation_details(ctx.page, limit=limit)
        )
        details = engine.session.call(lambda ctx: _embed_avatars(ctx.page, details, state.repository))
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.error("读取好友列表失败：%s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"读取好友列表失败：{type(exc).__name__}: {exc}\n"
                "常见原因：登录态失效（去账号页重新扫码）、Chromium 未安装、"
                "或网络访问不了抖音。"
            ),
        ) from exc

    return {
        "friends": details,
        "count": len(details),
    }


@router.get("/avatar")
def contact_avatar(
    request: Request,
    name: str = Query(..., min_length=1, max_length=120, description="好友名字（URL 编码）"),
) -> Response:
    """按需返回某个好友的头像字节，带浏览器缓存头。

    列表 / 卡片接口不再内嵌 base64，前端按需打这个接口；配合 ``Cache-Control``
    与 ``ETag``，同一头像在浏览器端只下载一次。

    路径安全：文件名由名字的 sha1 前缀决定（不直接拼名字），且只在该名字
    存在于好友档案时才返回，杜绝路径穿越与越权读取。
    """
    state = _state(request)
    repo = state.repository
    profiles = repo.load_friend_profiles()
    if name not in profiles:
        raise HTTPException(status_code=404, detail="没有该好友的头像缓存")
    data = repo.load_friend_avatar(name)
    if not data:
        raise HTTPException(status_code=404, detail="头像文件缺失，请重新同步好友")
    body, mime = data
    etag = '"' + hashlib.sha1(body).hexdigest()[:16] + '"'
    return Response(
        content=body,
        media_type=mime,
        headers={"Cache-Control": "public, max-age=86400", "ETag": etag},
    )


def _ensure_logged_in(state: Any) -> None:
    """给工作台引擎装载已保存的登录态（存在时）。

    为什么需要：工作台的引擎是 ``get_engine()`` 随用随启的新浏览器，
    **不会自动加载** ``data/accounts/*.state.json``。不装载的话，
    页面上全是登录窗 —— 表现为「登录态已失效」，但登录态文件明明好好的。
    实测踩过：好友列表接口第一次调用就 502。
    """
    repo = state.repository
    accounts = repo.list_accounts()
    if not accounts:
        return
    state_path = repo.account_state_path(accounts[0])
    if not state_path.exists():
        return

    engine = state.get_engine()
    engine.load_state(state_path)


def _embed_avatars(page: Any, details: list[dict[str, Any]], repo: Any) -> list[dict[str, Any]]:
    """把每个好友的头像图下载下来**存成文件**，并在详情里打上 has_avatar / avatar_ext。

    直接在前端 <img src="抖音CDN"> 会被防盗链拦（Referer 校验），
    所以用登录态的浏览器上下文代取。失败的头像标记 ``has_avatar=False``，
    前端用名字首字画占位头像 —— 一两个挂了不该让整个页面挂。

    ⚠️ 不再把 base64 塞进 details / friends.json：头像走按需接口
    ``/api/contacts/avatar?name=``，friends.json 只留轻量字段（避免随好友数膨胀）。
    """
    ext_map = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/webp": "webp",
        "image/gif": "gif",
    }
    for item in details:
        item.pop("avatar_data", None)
        item.pop("avatar_ext", None)
        item["has_avatar"] = False
        url = (item.get("avatar") or "").strip()
        name = item.get("name") or ""
        if not url or not name:
            continue
        try:
            resp = page.request.get(url, timeout=15_000)
            body = resp.body()
            if not body:
                continue
            mime = (resp.headers.get("content-type") or "image/jpeg").split(";")[0]
            ext = ext_map.get(mime, "jpg")
            repo.save_friend_avatar(name, body, ext)
            item["avatar_ext"] = ext
            item["has_avatar"] = True
        except Exception as exc:  # noqa: BLE001 —— 单个头像失败不影响整体
            LOGGER.debug("下载头像失败 %s：%s", url[:80], exc)
    return details


# =============================================================================
# 写
# =============================================================================


class ContactPayload(BaseModel):
    """一个收件人的可编辑字段。"""

    name: str = Field(min_length=1, max_length=80)
    conversation_id: str | None = None
    is_group: bool = False
    note: str | None = Field(default=None, max_length=200)
    enabled: bool = True
    weight: int = Field(default=1, ge=0, le=100)


@router.post("")
@router.post("/")
async def add_contact(request: Request, payload: ContactPayload) -> dict[str, Any]:
    """新增或更新一个收件人（按名字匹配）。"""
    state = _state(request)
    repo = state.repository

    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="名字不能为空")

    existing = {c.name: c for c in repo.load_contacts()}
    previous = existing.get(name)

    contact = Contact(
        name=name,
        conversation_id=payload.conversation_id,
        is_group=payload.is_group,
        note=payload.note,
        enabled=payload.enabled,
        weight=payload.weight,
        # 保留「今天已发」的记录 —— 改个备注不该让防重复失效
        last_sent_on=previous.last_sent_on if previous else None,
    )

    repo.upsert_contact(contact)

    return {
        "ok": True,
        "created": previous is None,
        "detail": f"{'已添加' if previous is None else '已更新'}：{name}",
        **_contacts_page(state),
    }


@router.patch("/{name}")
async def update_contact(request: Request, name: str, payload: ContactPayload) -> dict[str, Any]:
    """改一个已有收件人。名字本身也能改（会按新名字重新落盘）。"""
    state = _state(request)
    repo = state.repository

    existing = {c.name: c for c in repo.load_contacts()}
    if name not in existing:
        raise HTTPException(status_code=404, detail=f"没有叫「{name}」的收件人")

    old = existing[name]
    new_name = payload.name.strip()

    contact = Contact(
        name=new_name,
        conversation_id=payload.conversation_id,
        is_group=payload.is_group,
        note=payload.note,
        enabled=payload.enabled,
        weight=payload.weight,
        last_sent_on=old.last_sent_on,
    )

    if new_name != name:
        # 改名字等于「删旧的、加新的」，不然会留下两条
        repo.remove_contact(name)
    repo.upsert_contact(contact)

    return {
        "ok": True,
        "detail": f"已更新：{name}" + (f" → {new_name}" if new_name != name else ""),
        **_contacts_page(state),
    }


@router.delete("/{name}")
async def delete_contact(request: Request, name: str) -> dict[str, Any]:
    """删掉一个收件人。"""
    state = _state(request)
    repo = state.repository

    existing = {c.name for c in repo.load_contacts()}
    if name not in existing:
        raise HTTPException(status_code=404, detail=f"没有叫「{name}」的收件人")

    repo.remove_contact(name)
    LOGGER.info("已删除收件人：%s", name)

    return {
        "ok": True,
        "detail": f"已删除：{name}",
        **_contacts_page(state),
    }


class ResetTodayRequest(BaseModel):
    """清掉「今天已发」的标记。"""

    names: list[str] = Field(default_factory=list, description="留空表示清全部")
    confirm: bool = Field(default=False, description="必须为 true，防误触")


@router.post("/reset-today")
async def reset_today(request: Request, payload: ResetTodayRequest) -> dict[str, Any]:
    """把「今天已发送」的标记清掉，让防重复不再拦住这些人。

    **这是一个危险操作**：清掉之后定时任务会认为今天还没发过，
    于是可能真的再发一次。所以要求显式传 ``confirm=true``。

    它的正当用途：你手动在手机上发过了、但程序不知道，于是今天
    被跳过了，你想让它也走一遍（比如想验证链路）。
    """
    if not payload.confirm:
        raise HTTPException(
            status_code=422,
            detail=(
                "这个操作会让今天的防重复失效，可能重复发送。"
                "确认要这么做请传 confirm=true。"
            ),
        )

    state = _state(request)
    repo = state.repository

    contacts = list(repo.load_contacts())
    targets = set(payload.names) if payload.names else {c.name for c in contacts}

    cleared: list[str] = []
    updated = []
    for contact in contacts:
        if contact.name in targets and contact.last_sent_on:
            cleared.append(contact.name)
            updated.append(
                Contact(
                    name=contact.name,
                    conversation_id=contact.conversation_id,
                    is_group=contact.is_group,
                    note=contact.note,
                    enabled=contact.enabled,
                    weight=contact.weight,
                    last_sent_on=None,
                )
            )
        else:
            updated.append(contact)

    repo.save_contacts(tuple(updated))
    LOGGER.warning("已清除 %d 个收件人的今日发送标记", len(cleared))

    return {
        "ok": True,
        "cleared": cleared,
        "detail": f"已清除 {len(cleared)} 个收件人的今日标记",
        **_contacts_page(state),
    }


# =============================================================================
# 序列化
# =============================================================================


def _contact_payload(
    contact: Contact,
    today: str,
    profiles: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """前端要的字段。``sent_today`` 是算出来的，不落盘。

    ``streak`` / ``has_avatar`` 来自好友档案缓存（见 ``repo.load_friend_profiles``）——
    它们不是联系人本身的持久字段，而是「从抖音读到的展示信息」。
    头像不再内嵌 base64：列表只回 has_avatar 标记，真正要头像时前端走
    按需接口 ``/api/contacts/avatar?name=``。
    """
    profile = (profiles or {}).get(contact.name) or {}
    return {
        "name": contact.name,
        "conversation_id": contact.conversation_id,
        "is_group": contact.is_group,
        "note": contact.note,
        "enabled": contact.enabled,
        "weight": contact.weight,
        "last_sent_on": contact.last_sent_on,
        "sent_today": contact.sent_today(today),
        # 火花状态文本（纯天数 "849" 或 "重燃中 2/3"），读不到为空串
        "streak": profile.get("streak") or "",
        # 头像走按需接口 /api/contacts/avatar?name=，这里只回「有没有」标记
        "has_avatar": bool((profile or {}).get("avatar_ext")),
    }


def _sorted_contacts(contacts: tuple[Contact, ...] | list[Contact]) -> list[Contact]:
    """启用优先排序，其余保持原有顺序。

    用「是否启用 + 原下标」当键：稳定、可预测，且打开开关的人立刻浮到最前。
    """
    indexed = list(enumerate(contacts))
    indexed.sort(key=lambda pair: (0 if pair[1].enabled else 1, pair[0]))
    return [contact for _, contact in indexed]


def _contacts_page(
    state: Any,
    *,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    """构造分页后的收件人响应。

    ``contacts`` 字段只含**当前这一页**（带火花/头像）；其余是计数信息。
    写操作（增/删/改/同步）也复用它 —— 前端拿到第一页就够，避免每次
    都回传上百人的 base64 头像。
    """
    repo = state.repository
    contacts = repo.load_contacts()
    today = today_str()

    ordered = _sorted_contacts(contacts)
    total = len(ordered)

    page_size = max(1, min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, int(page or 1)), pages)

    start = (page - 1) * page_size
    window = ordered[start : start + page_size]

    profiles = repo.load_friend_profiles()
    return {
        "contacts": [_contact_payload(c, today, profiles) for c in window],
        "count": total,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "enabled_count": sum(1 for c in contacts if c.enabled),
        "sent_today_count": sum(1 for c in contacts if c.sent_today(today)),
        "today": today,
    }
