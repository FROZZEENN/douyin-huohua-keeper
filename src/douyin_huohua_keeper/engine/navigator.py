"""会话定位 —— 找到某个联系人的聊天窗口并打开它。

这是整个项目里**最容易失效**的一环，因为抖音的会话列表结构会变，
而且「匹配哪一行」这件事本身有歧义（「张三」和「张三丰」）。

两条通道：

- **主通道：读会话列表。** 直接读左侧列表里每一行的名字，匹配目标。
  不发起额外请求，行为特征最接近真人。
- **备用通道：搜索。** 在搜索框里输入名字，从结果里选。
  只在主通道找不到时启用 —— 因为搜索会产生额外的请求，是更明显的自动化痕迹。

匹配规则（严格程度递减）：
1. 完全相等
2. 去掉群组后缀后相等（处理「小明（3）」这类群名）
3. 去掉首尾空白和零宽字符后相等
4. 目标名是候选名的前缀，且候选名剩余部分是括号包起来的东西

**刻意不做模糊匹配。** 宁可报「找不到」，也不能发错人 —— 给错误的人发消息
是比失败更糟的结果。
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from . import selectors as sel

LOGGER = logging.getLogger(__name__)

# 零宽字符和方向控制符。从页面抠出来的文本经常混进这些，肉眼看不出来但比较会失败
_INVISIBLE_CHARS = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")

# 群组后缀：「小明（3）」「小明(3人)」「小明 [5]」
_GROUP_SUFFIX = re.compile(r"\s*[（(\[【]\s*\d+\s*[）)\]】]\s*$")


class ContactNotFoundError(LookupError):
    """在会话列表和搜索结果里都没找到目标联系人。"""

    def __init__(self, name: str, tried: list[str]) -> None:
        self.name = name
        self.tried = tried
        detail = "；".join(tried) if tried else "无"
        super().__init__(
            f"找不到联系人「{name}」。\n"
            f"已尝试：{detail}\n"
            "可能原因：名字与本机抖音里的显示名不一致、会话列表未加载完、"
            "或该好友最近没有任何聊天记录（会话列表默认只显示近期会话）。"
        )


class ConversationOpenError(RuntimeError):
    """找到了会话行但打开失败。"""


@dataclass(frozen=True, slots=True)
class ConversationEntry:
    """会话列表（或搜索结果）里的一行。"""

    name: str
    raw_text: str
    preview: str = ""
    index: int = -1
    # ``list`` = 来自左侧会话列表（index 是**当前 DOM 里的行下标**）；
    # ``search`` = 来自搜索框结果（index 是**搜索结果里的下标**，两者不是一个东西）。
    # 点击时必须按 source 选对应的定位方式，否则会点错行。
    source: str = "list"

    def normalized(self) -> str:
        return normalize_name(self.name)


def normalize_name(raw: str) -> str:
    """清理名字文本，让它能可靠比较。

    做三件事：
    - 去掉零宽字符（肉眼看不见，但会让 ``==`` 失败）
    - 合并连续空白
    - 去掉首尾空白
    """
    text = _INVISIBLE_CHARS.sub("", raw or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def strip_group_suffix(name: str) -> str:
    """去掉群名后面的成员数后缀。

    如果剥完之后什么都不剩（名字本身就是「（3）」这种），就原样返回 ——
    空名字会让后续的匹配判断全部失效，那不是我们想要的。
    """
    stripped = _GROUP_SUFFIX.sub("", name).strip()
    return stripped if stripped else name.strip()


def names_match(target: str, candidate: str) -> bool:
    """判断两个名字是否指向同一个人。

    这是整个定位逻辑的核心判断，所以单独抽出来便于测试。
    任何放宽匹配的改动都必须配上测试用例。
    """
    a = normalize_name(target)
    b = normalize_name(candidate)

    if not a or not b:
        return False

    # 1. 完全相等
    if a == b:
        return True

    # 2. 去掉群后缀后相等
    if strip_group_suffix(a) == strip_group_suffix(b):
        return True

    # 3. 候选带群后缀而目标不带（「小明（3）」匹配「小明」）
    if strip_group_suffix(b) == a:
        return True

    # 4. 反过来：目标带后缀而候选不带
    return strip_group_suffix(a) == b


def read_conversation_list(page: Any, *, limit: int = 60) -> tuple[ConversationEntry, ...]:
    """读取会话列表。

    返回的行顺序与页面显示顺序一致（最近的在前），这个顺序对
    「轮流发送」策略有影响，所以不能打乱。
    """
    # 列表容器**不是必需的**。
    #
    # 抖音的列表容器 class 是构建哈希名 —— ``[class*="conversationList"]``
    # 这类选择器实测一个都命中不了。而会话行本身带**稳定的语义标识**
    # ``[data-e2e="conversation-item"]``（实测命中 15 条）。
    #
    # 所以「有会话行、但没有可识别的容器」是真实页面的常态。
    # 以前这里因为找不到容器就直接返回空列表，导致「联系人明明在列表里
    # 却报找不到」—— 实测踩过。
    item_locator = _item_locator(page)
    if item_locator is None:
        return ()

    try:
        total = min(item_locator.count(), limit)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("读取会话行数量失败：%s", exc)
        return ()

    entries: list[ConversationEntry] = []
    for index in range(total):
        try:
            row = item_locator.nth(index)
            raw_text = (row.inner_text() or "").strip()
        except Exception:  # noqa: BLE001
            continue

        if not raw_text:
            continue

        name = _extract_name(row, raw_text)
        if not name:
            continue

        entries.append(
            ConversationEntry(
                name=name,
                raw_text=raw_text,
                preview=_extract_preview(row, raw_text, name),
                index=index,
            )
        )

    LOGGER.debug("读取到 %d 个会话", len(entries))
    return tuple(entries)


def read_conversation_details(
    page: Any, *, limit: int = 300, expand: bool = True
) -> tuple[dict[str, Any], ...]:
    """读取会话列表的**富信息**：名字 + 火花状态 + 头像地址。

    给工作台的「好友列表」页用 —— 那里要展示每个人头上的火焰 🔥
    和连续天数，和手机上看到的一样。

    返回字典字段：
    - ``name``:     干净的名字（与 ``read_conversation_list`` 同源）
    - ``streak``:   火花文本，纯天数（"849"）或恢复态（"重燃中 2/3"），
                    读不到为 ``""``
    - ``avatar``:   头像图片的 URL（交给上层下载转 base64；读不到为 ``""``）
    - ``is_group``: 是否群聊（名字带「（3）」后缀，或行内有群头像/群标识）

    头像 URL 是抖音 CDN 的地址，直接在前端 <img> 里引用可能被防盗链拦掉，
    所以这里只负责取地址，下载由路由层用**已登录的浏览器上下文**完成。

    ``expand=True``（默认）会滚动遍历整个虚拟列表，采集**全部**会话；
    设成 ``False`` 只读当前这一屏（约 15 条），用于「只要眼前这批」的场景。
    """
    if expand:
        return collect_conversation_details(page, limit=limit)

    entries = _details_from_rows(_conversation_rows(page, limit=limit))
    LOGGER.debug("读取到 %d 个会话（含火花/头像）", len(entries))
    return tuple(entries)


def _detect_group(row: Any, name: str, raw_text: str) -> bool:
    """判断一行会话是不是群聊。

    两个信号，命中任意一个即为群：

    1. 名字带成员数后缀：「小明（3）」「阿强(5人)」；
    2. 行内有群头像 / 群标识容器（见 ``sel.GROUP_MARKERS``）；
    3. 行文本含群专属字样：「1人已读」「[有人@我]」等（见 ``sel.GROUP_TEXT_MARKERS``）。

    刻意保守：**宁可漏判成单聊，也不要错判成群** —— 把单聊标上「群聊」
    会让用户莫名其妙，而漏标最多是少个标签。
    """
    if _GROUP_SUFFIX.search(name or ""):
        return True

    # 群名常被渲染成「张三, 李四, 王五…」这种多人并列（实测：群会话名就是成员名拼的）。
    # 用「≥2 个逗号」判断 —— 单个人的昵称里几乎不会出现两个以上逗号。
    if len(re.findall(r"[,，]", name or "")) >= 2:
        return True

    for candidate in sel.GROUP_MARKERS:
        try:
            if row.locator(candidate).count() > 0:
                return True
        except Exception:  # noqa: BLE001 —— 单个候选失配换下一个
            continue

    text = raw_text or ""
    return any(marker in text for marker in sel.GROUP_TEXT_MARKERS)


def _item_locator(page: Any) -> Any:
    """定位会话行，返回能命中的 locator；找不到返回 ``None``。

    **列表容器不是必需的**：抖音的容器 class 是构建哈希名
    （``[class*="conversationList"]`` 一个都命中不了），而会话行本身带稳定的
    ``[data-e2e="conversation-item"]``。所以「有行、没有可识别容器」是常态，
    以前因为找不到容器就直接返回空列表，导致「联系人明明在列表里却报找不到」。
    """
    container = sel.first_present(page, sel.CONVERSATION_LIST)
    if container is None:
        LOGGER.debug(
            "没找到会话列表容器（试过 %s），改为在全页直接找会话行",
            sel.describe_candidates(sel.CONVERSATION_LIST),
        )

    # 有容器就在容器里找（范围更准），没有就退回全页找
    scope = container if container is not None else page

    for candidate in sel.CONVERSATION_ITEM:
        try:
            probe = scope.locator(candidate)
            if probe.count() > 0:
                return probe
        except Exception:  # noqa: BLE001 —— 单个候选失配换下一个
            continue

    LOGGER.warning("没找到可识别的会话行，试过：%s", sel.describe_candidates(sel.CONVERSATION_ITEM))
    return None


def _conversation_rows(page: Any, *, limit: int) -> list[Any]:
    """拿会话行的定位器列表。"""
    probe = _item_locator(page)
    if probe is None:
        return []
    try:
        total = min(probe.count(), limit)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("读取会话行数量失败：%s", exc)
        return []
    return [probe.nth(index) for index in range(total)]


# 会话列表是**虚拟列表**（windowing）：DOM 里始终只有约 15 行，滚动时行元素被
# 循环复用、内容换掉。所以「滚到底再读一次」这种写法是错的 —— 那样只会读到
# 最底部那一屏，中间的全会漏掉。正确做法是**一边滚一边把每一屏采集累加**。
#
# 实测（抖音 2026-09）：滚动容器是 ``.conversationConversationListwrapper``
# （overflowY: scroll，clientHeight≈840，scrollHeight 随加载从 3015 涨到 10720），
# 而 ``[data-e2e="conversation-item"]`` 的数量**始终 ≈15**。
_SCROLL_STEP_JS = """
(el) => {
  let node = el && el.parentElement;
  while (node && node !== document.body) {
    const style = window.getComputedStyle(node);
    const oy = style.overflowY;
    if ((oy === 'auto' || oy === 'scroll') && node.scrollHeight > node.clientHeight + 4) {
      const before = node.scrollTop;
      // 按「一屏的 85%」滚动，而不是直接跳到 scrollHeight ——
      // 虚拟列表直接跳到底会**跳过中间行**，那些行就永远采不到了。
      node.scrollTop = Math.min(before + node.clientHeight * 0.85, node.scrollHeight);
      return { before: before, after: node.scrollTop, scrollHeight: node.scrollHeight };
    }
    node = node.parentElement;
  }
  return null;
}
"""


# 把会话列表滚回顶部。
#
# ⚠️ 必须有这一步：一次任务里要给多个人发消息，而**滚动位置会残留**。
#    上一位联系人是滚到一半才找到的，下一位就会「看不到顶部那几行」——
#    表现就是莫名其妙地「找不到联系人」。
_SCROLL_TOP_JS = """
(el) => {
  let node = el && el.parentElement;
  while (node && node !== document.body) {
    const style = window.getComputedStyle(node);
    const oy = style.overflowY;
    if ((oy === 'auto' || oy === 'scroll') && node.scrollHeight > node.clientHeight + 4) {
      node.scrollTop = 0;
      return true;
    }
    node = node.parentElement;
  }
  return false;
}
"""


def _scroll_to_top(page: Any, probe: Any) -> None:
    """把会话列表滚回顶部（失败不致命，退回鼠标滚轮）。"""
    try:
        if probe.count() > 0:
            probe.nth(0).evaluate(_SCROLL_TOP_JS)
            return
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("滚回顶部失败（改用鼠标滚轮）：%s", exc)
    with contextlib.suppress(Exception):
        page.mouse.wheel(0, -30000)


def _scroll_find(
    page: Any,
    matcher: Any,
    *,
    max_rounds: int = 25,
    pause_ms: int = 250,
) -> ConversationEntry | None:
    """从顶部开始**逐屏向下**找，命中就返回「当前这一屏」里的 entry。

    返回的 entry 带的是**当前 DOM 里的下标**，可以立刻拿去点击。

    这是修「找不到联系人」的关键：
    会话列表是**虚拟列表**，DOM 里永远只有十几行；原来只读一次「前 60 条」
    （实际就是当前这一屏），下面的人一律找不到。而每次发完消息，
    那个人会被顶到最上面，**把还没轮到的人越挤越往下** ——
    于是「发了一部分、剩下全报找不到联系人」。（实测事故，4/15 失败。）
    """
    probe = _item_locator(page)
    if probe is None:
        return None

    _scroll_to_top(page, probe)
    with contextlib.suppress(Exception):
        page.wait_for_timeout(pause_ms)

    stable = 0
    for _ in range(max(1, max_rounds)):
        probe = _item_locator(page)
        if probe is None:
            return None
        try:
            count = probe.count()
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("读取会话行数量失败：%s", exc)
            return None
        if count <= 0:
            return None

        for index in range(count):
            try:
                row = probe.nth(index)
                raw_text = (row.inner_text() or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if not raw_text:
                continue
            name = _extract_name(row, raw_text)
            if not name or not matcher(name):
                continue
            return ConversationEntry(
                name=name,
                raw_text=raw_text,
                preview=_extract_preview(row, raw_text, name),
                index=index,
            )

        state = _scroll_step(page, probe, count)
        with contextlib.suppress(Exception):
            page.wait_for_timeout(pause_ms)

        if _at_bottom(state):
            stable += 1
            if stable >= 2:
                return None
        else:
            stable = 0

    return None


def scroll_conversation_list_to_top(page: Any) -> None:
    """把会话列表滚回顶部（对外暴露，发送确认也要用）。

    ⚠️ 发送成功后，那个会话会**跳到列表最前面**。如果列表还停在
    「刚才滚下去找人」的位置，会话列表预览就会读到别人那一屏、
    根本找不到目标 —— 于是**明明发出去了却报「未能确认」**。
    实测事故：4 个位置较深的会话补发后全部误报「结果不确定」，
    而它们其实都到了（消息区里是「刚刚 faze up」）。
    """
    probe = _item_locator(page)
    if probe is not None:
        _scroll_to_top(page, probe)


def _find_index_in_scope(probe: Any, name: str) -> int:
    """在当前这一屏的会话行里按名字找下标；找不到返回 -1。"""
    try:
        count = probe.count()
    except Exception:  # noqa: BLE001
        return -1
    wanted = normalize_name(name)
    for index in range(min(count, 60)):
        try:
            row = probe.nth(index)
            raw_text = (row.inner_text() or "").strip()
        except Exception:  # noqa: BLE001
            continue
        if not raw_text:
            continue
        row_name = _extract_name(row, raw_text)
        if row_name and normalize_name(row_name) == wanted:
            return index
    return -1


def _row_matches(probe: Any, index: int, name: str) -> bool:
    """第 index 行是不是就是「name」—— 点击前最后一道核对。"""
    try:
        row = probe.nth(index)
        raw_text = (row.inner_text() or "").strip()
    except Exception:  # noqa: BLE001
        return False
    if not raw_text:
        return False
    row_name = _extract_name(row, raw_text)
    return bool(row_name) and names_match(name, row_name)


def _details_from_rows(rows: list[Any]) -> list[dict[str, Any]]:
    """把一批会话行解析成富信息字典（不滚动，只解析当前这一批）。"""
    entries: list[dict[str, Any]] = []
    for row in rows:
        raw_text = (row.inner_text() or "").strip()
        name = _extract_name(row, raw_text)
        if not name:
            continue
        entries.append(
            {
                "name": name,
                "streak": _first_text(row, sel.STREAK_CANDIDATES),
                "avatar": _first_attr(row, sel.AVATAR_CANDIDATES, "src"),
                "is_group": _detect_group(row, name, raw_text),
            }
        )
    return entries


def collect_conversation_details(
    page: Any,
    *,
    limit: int = 300,
    max_rounds: int = 120,
    pause_ms: int = 400,
    settle_rounds: int = 3,
) -> tuple[dict[str, Any], ...]:
    """滚动遍历虚拟列表，采集**全部**会话的富信息（按出现顺序去重）。

    这是读全好友列表的正确做法：

    - 每一步只读「当前这一屏」的行；
    - 逐步向下滚（一屏的 85%），把新出现的名字累加进结果；
    - 连续 ``settle_rounds`` 步滚不动（到底了）时结束。

    不这么做的话，单次读取只能拿到首屏约 15 条 —— 这正是
    「手机里好友一大串，同步进收件人页却只有十几个」的根因。
    """
    order: list[str] = []
    seen: dict[str, dict[str, Any]] = {}
    stable = 0

    for _ in range(max(1, max_rounds)):
        probe = _item_locator(page)
        if probe is None:
            break

        try:
            count = probe.count()
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("读取会话行数量失败：%s", exc)
            break
        if count <= 0:
            break

        rows = [probe.nth(index) for index in range(count)]
        for item in _details_from_rows(rows):
            if item["name"] in seen:
                continue
            seen[item["name"]] = item
            order.append(item["name"])
            if len(order) >= limit:
                break

        if len(order) >= limit:
            break

        state = _scroll_step(page, probe, count)
        with contextlib.suppress(Exception):
            page.wait_for_timeout(pause_ms)

        if _at_bottom(state):
            stable += 1
            if stable >= settle_rounds:
                break
        else:
            stable = 0

    LOGGER.debug("遍历会话列表完成：采集到 %d 个会话", len(order))
    return tuple(seen[name] for name in order)


def _scroll_step(page: Any, probe: Any, count: int) -> Any:
    """向下滚一屏，返回滚动状态（用于判断是否到底）；失败返回 None。"""
    if count <= 0:
        return None
    try:
        return probe.nth(count - 1).evaluate(_SCROLL_STEP_JS)
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("滚动会话列表失败（改用鼠标滚轮）：%s", exc)
    with contextlib.suppress(Exception):
        page.mouse.wheel(0, 800)
    return None


def _at_bottom(state: Any) -> bool:
    """根据滚动状态判断是否已经到底。

    ``state`` 为 None（没找到可滚动祖先 / 页面对象不支持 evaluate）时也算「到底」——
    通常意味着列表很短、没有滚动条；连续几轮后自然收尾。
    """
    if not isinstance(state, dict):
        return True
    before = state.get("before")
    after = state.get("after")
    if before is None or after is None:
        return True
    return abs(float(after) - float(before)) < 2


def _first_text(row: Any, candidates: tuple[str, ...]) -> str:
    """在行内按候选顺序找第一个非空文本。"""
    for candidate in candidates:
        try:
            matches = row.locator(candidate)
            total = min(matches.count(), 5)
        except Exception:  # noqa: BLE001
            continue
        for index in range(total):
            try:
                text = (matches.nth(index).inner_text() or "").strip()
            except Exception:  # noqa: BLE001
                continue
            if text:
                return normalize_name(text)
    return ""


def _first_attr(row: Any, candidates: tuple[str, ...], attr: str) -> str:
    """在行内按候选顺序找第一个带该属性的元素，返回属性值。"""
    for candidate in candidates:
        try:
            matches = row.locator(candidate)
            total = min(matches.count(), 5)
        except Exception:  # noqa: BLE001
            continue
        for index in range(total):
            try:
                value = matches.nth(index).get_attribute(attr)
            except Exception:  # noqa: BLE001
                continue
            if value:
                return value.strip()
    return ""


def _list_probe(page: Any) -> Any:
    """拿到「会话行」的 locator；找不到返回 None。

    **列表容器不是必需的**：抖音的容器 class 是哈希名（一个都命中不了），
    而会话行有稳定的 ``[data-e2e="conversation-item"]``。
    以前找不到容器就直接抛「会话列表消失了」—— 明明已经匹配到目标，却倒在这一步。
    """
    container = sel.first_present(page, sel.CONVERSATION_LIST)
    if container is None:
        LOGGER.debug(
            "没找到会话列表容器（试过 %s），改为在全页直接定位会话行",
            sel.describe_candidates(sel.CONVERSATION_LIST),
        )
    scope = container if container is not None else page

    for candidate in sel.CONVERSATION_ITEM:
        try:
            probe = scope.locator(candidate)
            if probe.count() > 0:
                return probe
        except Exception:  # noqa: BLE001
            continue
    return None


def _click_list_row(page: Any, entry: ConversationEntry, *, timeout_ms: int) -> None:
    """点击会话列表里的那一行。

    ⚠️ 点之前**核对行名字**：列表可能在「读」和「点」之间刷新过
    （我们一边滚动一边读，行是会被复用的）。发错人比失败严重得多。
    """
    probe = _list_probe(page)
    if probe is None:
        raise ConversationOpenError("找不到会话行，列表可能已刷新")

    index = entry.index
    if not _row_matches(probe, index, entry.name):
        found = _find_index_in_scope(probe, entry.name)
        if found < 0:
            raise ConversationOpenError(f"列表已刷新，第 {entry.index} 行不再是「{entry.name}」")
        LOGGER.debug("下标对不上，按名字重新定位：第 %d -> 第 %d 行", entry.index, found)
        index = found

    try:
        probe.nth(index).click(timeout=timeout_ms)
    except Exception as exc:
        raise ConversationOpenError(
            f"点击会话「{entry.name}」失败：{type(exc).__name__}: {exc}"
        ) from exc


def _click_search_row(page: Any, entry: ConversationEntry, *, timeout_ms: int) -> None:
    """点击**搜索结果**里的那一行。

    ⚠️ 搜索结果的下标和会话列表的下标不是一回事 —— 以前把搜索结果按
    「会话列表第 N 行」去点，必然点错。两条通道必须分开处理。
    """
    items = sel.first_present(page, sel.SEARCH_RESULT_ITEM_CANDIDATES)
    if items is None:
        raise ConversationOpenError("搜索结果不见了，无法打开会话")

    try:
        total = min(items.count(), 20)
    except Exception:  # noqa: BLE001
        total = 1

    for index in range(max(1, total)):
        try:
            row = items.nth(index) if total > 1 else items
            text = (row.inner_text() or "").strip()
        except Exception:  # noqa: BLE001
            continue
        if not text:
            continue
        name = text.split("\n")[0].strip()
        if not names_match(entry.name, name):
            continue
        try:
            row.click(timeout=timeout_ms)
        except Exception as exc:
            raise ConversationOpenError(f"点击搜索结果「{name}」失败：{exc}") from exc
        return

    raise ConversationOpenError(f"搜索结果里找不到「{entry.name}」")


def clear_search_box(page: Any) -> None:
    """清空搜索框（用过搜索后**必须**清）。

    搜索结果是显示在左侧列表位置上的。残留的搜索结果会让下一位联系人
    的「读会话列表」读到一堆搜索结果 → 又变成「找不到联系人」。
    这是搜索这条备用通道唯一的坑。
    """
    box = sel.first_present(page, sel.SEARCH_INPUT_CANDIDATES)
    if box is None:
        return
    with contextlib.suppress(Exception):
        box.fill("")
        box.press("Escape")


def open_conversation(page: Any, entry: ConversationEntry, *, timeout_ms: int = 10_000) -> None:
    """点击会话行，打开聊天窗口。

    点击后要**确认真的切换过去了** —— 只点一下不验证，会出现
    「以为打开了 A 的会话，实际消息发给了 B」这种最糟糕的失败。
    """
    if entry.source == "search":
        _click_search_row(page, entry, timeout_ms=timeout_ms)
    else:
        _click_list_row(page, entry, timeout_ms=timeout_ms)

    # 等待头部切换到目标名字
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        header = read_active_conversation_name(page)
        if header and names_match(entry.name, header):
            LOGGER.debug("已打开会话：%s", header)
            return
        time.sleep(0.2)

    header = read_active_conversation_name(page)
    raise ConversationOpenError(
        f"点击了「{entry.name}」但会话头部显示的是「{header or '（读不到）'}」。"
        "为避免发错人，本次发送已中止。"
    )


def read_active_conversation_name(page: Any) -> str | None:
    """读当前打开会话的对象名字。

    和 ``_extract_name`` 同一原则：**取候选里最短的那个非空文本**。

    原因是实测出来的：头部容器 ``RightPanelHeaderinfoContainer`` 里
    也含有同样的名字文字，而真正的名字在更内层的叶子节点
    ``RightPanelHeadertitle`` 里。取到外层容器可能连带读到别的内容，
    叶子节点文本最短、最干净。
    """
    for candidate in sel.CHAT_HEADER_NAME:
        try:
            matches = page.locator(candidate)
            total = min(matches.count(), 5)
        except Exception:  # noqa: BLE001
            continue

        best = ""
        for index in range(total):
            try:
                text = normalize_name(matches.nth(index).inner_text() or "")
            except Exception:  # noqa: BLE001
                continue
            if not text:
                continue
            if not best or len(text) < len(best):
                best = text

        if best:
            return best

    return None


def search_conversation(
    page: Any,
    target: str,
    *,
    timeout_ms: int = 8_000,
) -> ConversationEntry | None:
    """备用通道：用搜索框找联系人。

    只在会话列表找不到时使用。会产生额外的搜索请求，所以不默认启用。
    """
    search_box = sel.first_present(page, sel.SEARCH_INPUT_CANDIDATES)
    if search_box is None:
        LOGGER.debug("没有搜索框，备用通道不可用")
        return None

    try:
        search_box.click(timeout=timeout_ms)
        search_box.fill("")
        search_box.fill(target)
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("搜索框操作失败：%s", exc)
        return None

    # 等结果出现
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        result_item = sel.first_present(page, sel.SEARCH_RESULT_ITEM_CANDIDATES)
        if result_item is not None:
            try:
                count = min(result_item.count() if hasattr(result_item, "count") else 1, 20)
            except Exception:  # noqa: BLE001
                count = 1

            for index in range(count):
                try:
                    row = result_item.nth(index) if count > 1 else result_item
                    text = (row.inner_text() or "").strip()
                except Exception:  # noqa: BLE001
                    continue
                if not text:
                    continue
                name = text.split("\n")[0].strip()
                if names_match(target, name):
                    return ConversationEntry(
                        name=name, raw_text=text, index=index, source="search"
                    )
        time.sleep(0.2)

    return None


def _locate_in_list(page: Any, target: str) -> ConversationEntry | None:
    """在会话列表里**滚着找**。精确匹配优先，再放宽；整轮找不到就重扫一遍。

    ⚠️ 为什么必须滚动：会话列表是**虚拟列表**，DOM 里永远只有十几行。
    只读一次等于只读一屏 —— 而每发完一个人，那个人就被顶到最上面，
    **把还没轮到的人越挤越往下**，很快挤出屏幕 → 「找不到联系人」。
    （实测事故：15 个人里 4 个报这个，全是真的没发出去。）

    ⚠️ 为什么要重扫一遍：列表与页面渲染都是异步的。实测「同一份登录态、
    同一个联系人，手动多等几秒能匹配上，脚本立刻读却报找不到」。
    多花几秒的成本极低，而误判「联系人不存在」会**直接少发一个人**。
    """
    wanted = normalize_name(target)

    for attempt in range(2):
        entry = _scroll_find(page, lambda name: normalize_name(name) == wanted)
        if entry is not None:
            LOGGER.info("会话列表命中（精确）：%s", entry.name)
            return entry

        entry = _scroll_find(page, lambda name: names_match(target, name))
        if entry is not None:
            LOGGER.info("会话列表命中（宽松）：%s", entry.name)
            return entry

        if attempt == 0:
            LOGGER.info("第一轮没找到「%s」，等 2 秒后把列表重扫一遍", target)
            time.sleep(2.0)

    return None


def locate_conversation(
    page: Any,
    target: str,
    *,
    allow_search: bool = True,
    limit: int = 60,
) -> ConversationEntry:
    """定位并打开目标会话。这是对外的统一入口。

    查找顺序（可靠性递减）：

    1. **会话列表里逐屏滚动找** —— 主通道，覆盖绝大多数情况；
    2. **搜索框**（``allow_search=True``）—— 列表里确实没有时用。
       会产生额外请求，所以用完**必须清空搜索框**（否则会污染下一位的列表读取）。

    找不到时抛 :class:`ContactNotFoundError`，并把尝试过的通道记录下来 ——
    报错信息里带上「试过什么」是排障效率的关键。
    """
    tried: list[str] = []

    entry = _locate_in_list(page, target)
    if entry is not None:
        open_conversation(page, entry)
        return entry

    tried.append("会话列表（已从头滚到底，未找到）")

    if allow_search:
        entry = search_conversation(page, target)
        if entry is not None:
            LOGGER.info("搜索命中：%s", entry.name)
            try:
                open_conversation(page, entry)
            finally:
                clear_search_box(page)
            return entry
        tried.append("搜索框（未找到匹配项）")

    raise ContactNotFoundError(target, tried)


def list_contact_names(page: Any, *, limit: int = 300, expand: bool = True) -> tuple[str, ...]:
    """列出会话列表里的所有名字。工作台「同步会话列表」用它。

    ``expand=True`` 会滚动遍历虚拟列表采集全部 —— 否则长列表只能读到首屏十几条。
    """
    if expand:
        return tuple(item["name"] for item in collect_conversation_details(page, limit=limit))
    return tuple(entry.name for entry in read_conversation_list(page, limit=limit))


# =============================================================================
# 内部
# =============================================================================


def _extract_name(row: Any, raw_text: str) -> str:
    """从会话行里抠出名字。

    ⚠️ 关键细节：**在候选元素里挑「最短的那个非空文本」，而不是取第一个。**

    实测（抖音 2026-09）``[class*="title"]`` 在每一行里会命中 **2 个**元素：

        外层容器: '阿明\\n849\\n15分钟前'   ← 名字 + 火花天数 + 时间
        内层叶子: '阿明'                    ← 真正的名字

    取 ``.first`` 会拿到外层那个，于是解析出的名字变成「阿明 849 15分钟前」，
    **永远匹配不上用户填的「阿明」** —— 表现为「联系人明明在会话列表里，
    却报找不到」。实测就是这个问题。

    名字元素是叶子节点，文本必然最短，所以用「最短非空」来挑最稳。
    """
    for candidate in sel.CONVERSATION_NAME:
        try:
            matches = row.locator(candidate)
            total = min(matches.count(), 10)
        except Exception:  # noqa: BLE001
            continue

        best = ""
        for index in range(total):
            try:
                text = normalize_name(matches.nth(index).inner_text() or "")
            except Exception:  # noqa: BLE001
                continue
            if not text:
                continue
            if not best or len(text) < len(best):
                best = text

        if best:
            return best

    # 退化：整行文本按行切，第一行通常是名字
    lines = [line.strip() for line in (raw_text or "").split("\n") if line.strip()]
    return normalize_name(lines[0]) if lines else ""


def _extract_preview(row: Any, raw_text: str, name: str) -> str:
    """抠出最后一条消息的预览文本。发送确认会用到它。"""
    for candidate in sel.CONVERSATION_PREVIEW:
        try:
            locator = row.locator(candidate).first
            if locator.count() > 0:
                text = (locator.inner_text() or "").strip()
                if text and text != name:
                    return normalize_name(text)
        except Exception:  # noqa: BLE001
            continue

    # 退化：整行去掉名字之后的剩余部分
    remainder = (raw_text or "").replace(name, "", 1).strip()
    lines = [line.strip() for line in remainder.split("\n") if line.strip()]
    return normalize_name(lines[0]) if lines else ""
