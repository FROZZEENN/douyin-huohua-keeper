"""发送确认状态机。

这个模块解决一个具体问题：**按了发送之后，怎么知道消息真的发出去了？**

天真的做法是「点了发送按钮就算成功」。这是错的，原因：
- 点击可能没生效（元素被遮挡、页面还在加载）
- 消息可能被服务端拒绝（频率限制、内容审核）
- 网络中断时页面会显示失败重试标记

所以这里的做法是**观察终态**：发送后轮询会话列表，看该联系人的
最后一条消息预览是否变成了我们刚发的内容。

三个状态：
- ``OBSERVING``   刚发出，等待页面出现变化
- ``STABILIZING`` 看到了变化，再等一会儿确认它不是瞬时的
- ``CONFIRMED``   连续多次观察到一致的结果，判定成功

为什么要等两次：页面从「输入框清空」到「会话列表更新」之间有延迟，
如果只看到一次变化就宣布成功，可能在页面回滚时误报。

**宁可报错也不误报成功。** 误报成功的代价是火花静默断掉且没人知道，
而报错最多是多一条告警。
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass
from typing import Any

from ..models import FailureKind, RunStatus
from . import selectors as sel
from .navigator import normalize_name, read_conversation_list, scroll_conversation_list_to_top

LOGGER = logging.getLogger(__name__)

# 默认超时（毫秒），来自配置但这里给个兜底
DEFAULT_CONFIRM_TIMEOUT_MS = 15_000

# 轮询间隔（秒）
POLL_INTERVAL_SEC = 0.4

# 需要连续观察到多少次一致结果才判定成功
STABLE_OBSERVATIONS = 2


class ConfirmState(str, enum.Enum):
    """确认状态机的状态。"""

    OBSERVING = "observing"
    STABILIZING = "stabilizing"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class ConfirmOutcome:
    """确认结果。"""

    status: RunStatus
    state: ConfirmState
    detail: str
    failure_kind: FailureKind | None = None
    observed_preview: str = ""
    elapsed_seconds: float = 0.0

    @property
    def confirmed(self) -> bool:
        return self.state is ConfirmState.CONFIRMED


@dataclass(frozen=True, slots=True)
class ConversationSnapshot:
    """发送**之前**对会话的观察，用于「到底有没有变化」的比对。"""

    preview: str = ""
    index: int = -1
    raw_text: str = ""
    message_count: int = -1
    # 消息区里「正好等于要发的内容」的行数（-1 = 没读到）
    needle_count: int = -1

    @property
    def usable(self) -> bool:
        return self.index >= 0 or bool(self.preview) or self.message_count >= 0


def snapshot_conversation(page: Any, contact_name: str, *, expected_text: str = "") -> ConversationSnapshot:
    """抓一份会话的基线快照（**发送前**调用）。

    这是「不误报成功」的关键：只有相对基线**比出变化**，才能证明列表真的更新了。
    抓不到也不致命 —— 只是这次少一个判据，判定会更保守（更倾向于报 UNCERTAIN）。

    ``expected_text`` 给定时会额外数一遍「消息区里已经是这条内容的有几行」，
    这样发送后再数一次、比出差值，就能确认「确实新增了一条」——
    这条判据与**会话列表预览**无关，因此不受「抖音把预览替换成系统提示」影响。
    """
    entry = None
    try:
        entry = _read_entry_for(page, contact_name)
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("抓会话基线失败：%s", exc)

    return ConversationSnapshot(
        preview=(entry.preview if entry else "") or "",
        index=(entry.index if entry else -1),
        raw_text=(entry.raw_text if entry else "") or "",
        message_count=_count_messages(page),
        needle_count=_count_message_lines(page, expected_text),
    )


def confirm_sent(
    page: Any,
    *,
    contact_name: str,
    expected_text: str,
    timeout_ms: int = DEFAULT_CONFIRM_TIMEOUT_MS,
    poll_interval: float = POLL_INTERVAL_SEC,
    require_stable: int = STABLE_OBSERVATIONS,
    baseline: ConversationSnapshot | None = None,
) -> ConfirmOutcome:
    """确认消息已送达。

    ⚠️ **必须观察到「页面发生了变化」才判成功。** 这是 2026-09 修的一次真实事故：

        消息内容恒为「1」，而会话列表里的预览**本来就是「1」**（昨天发的那条）。
        旧实现只判断「预览 == 期望内容」，于是无论消息有没有真的发出去，
        都会立刻判成功 —— 实测出现过「工作台显示已发送、对方什么都没收到」。

    现在的判据是「预览匹配期望内容」**并且**至少满足一条变化证据：

    - ``preview_changed``：预览和发送前不一样了（对内容无关，适用面最广）；
    - ``moved_to_top``：该会话从列表中部跳到了第 1 个（新消息会把会话顶到最前）；
    - ``message_grew``：会话内的消息条数变多了（与内容无关，最适用于恒定文案）。

    三条都拿不到、但输入框已清空 → ``UNCERTAIN``（**不重试**，避免重复发送）。
    超时且输入框还有内容 → ``FAILED``。

    原则：**宁可报「不确定」，也绝不误报成功** —— 误报成功意味着火花静默断掉
    且没人知道；报不确定最多是多一条提示。
    """
    started = time.monotonic()
    deadline = started + timeout_ms / 1000
    expected = normalize_name(expected_text)
    base = baseline or ConversationSnapshot()

    stable_hits = 0
    last_preview = ""
    saw_input_cleared = False
    saw_change = False

    # 发送成功后会话会跳到列表**最前面**。如果列表还停在「刚才滚下去找人」的位置，
    # 我们读到的预览是别人那一屏、根本找不到目标 ——
    # 于是明明发出去了却报「未能确认」（实测：4 个位置较深的会话全被误报）。
    try:
        scroll_conversation_list_to_top(page)
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("把会话列表滚回顶部失败（不影响判定）：%s", exc)

    while time.monotonic() < deadline:
        entry = None
        try:
            entry = _read_entry_for(page, contact_name)
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("读取会话失败：%s", exc)

        preview = (entry.preview if entry else "") or ""
        index = entry.index if entry else -1
        if preview:
            last_preview = preview

        # 证据一（最可靠、与预览无关、恒定文案也能用）：
        # 消息区里「正好是这条内容」的行数变多了 → 确实新增了一条气泡。
        needle_now = -1
        if base.needle_count >= 0 or expected_text:
            needle_now = _count_message_lines(page, expected_text)
        needle_grew = base.needle_count >= 0 and needle_now > base.needle_count

        # 证据二（会话列表预览）：要求预览匹配期望内容，**且**相对基线有变化
        preview_ok = bool(preview and expected and _preview_matches(preview, expected))
        preview_changed = preview_ok and _changed_vs_baseline(page, preview, index, base)

        if needle_grew or preview_changed:
            saw_change = True
            stable_hits += 1
            if stable_hits >= require_stable:
                elapsed = time.monotonic() - started
                why = "消息区里新增了这条内容" if needle_grew else "会话列表已更新"
                return ConfirmOutcome(
                    status=RunStatus.SUCCESS,
                    state=ConfirmState.CONFIRMED,
                    detail=f"已确认送达：{why}（用时 {elapsed:.1f}s）",
                    observed_preview=preview,
                    elapsed_seconds=elapsed,
                )
        else:
            # 没看到任何变化 —— 计数归零，避免累计到「零星的几次」
            stable_hits = 0

        if _has_failure_marker(page):
            elapsed = time.monotonic() - started
            return ConfirmOutcome(
                status=RunStatus.FAILED,
                state=ConfirmState.FAILED,
                detail="页面上出现了发送失败标记",
                failure_kind=FailureKind.TRANSIENT,
                elapsed_seconds=elapsed,
            )

        # 弱证据：输入框被清空说明页面接受了一次发送动作
        if not saw_input_cleared and _is_input_cleared(page):
            saw_input_cleared = True
            LOGGER.debug("输入框已清空，继续等待会话列表更新")

        time.sleep(poll_interval)

    elapsed = time.monotonic() - started

    # 超时了。三种情况分开说清楚，因为「该不该重试」完全不同。
    #
    # ⚠️ 这一条是新增的、也是最重要的：发送前的预览**本来就等于**要发的内容
    #    （恒定文案的典型情形）。此时会话列表根本提供不了证据，
    #    必须明确说「无法确认」，而不是默默当成功。
    if base.preview and expected and _preview_matches(base.preview, expected) and not saw_change:
        return ConfirmOutcome(
            status=RunStatus.UNCERTAIN,
            state=ConfirmState.UNCERTAIN,
            detail=(
                "会话列表里本来就显示着和这条相同的内容（上一次发的），"
                f"而 {elapsed:.0f}s 内没有观察到任何变化，**无法确认这次是否真的发出去了**。"
                "消息可能已发出，因此不会自动重试（避免重复发送）。请手动看一眼会话确认。"
            ),
            failure_kind=FailureKind.TRANSIENT,
            observed_preview=last_preview,
            elapsed_seconds=elapsed,
        )

    if saw_input_cleared:
        return ConfirmOutcome(
            status=RunStatus.UNCERTAIN,
            state=ConfirmState.UNCERTAIN,
            detail=(
                f"输入框已清空但 {elapsed:.0f}s 内没能在会话里确认到这条消息"
                "（消息区也没读到新增）。消息**可能已发出**，因此不会自动重试"
                "（避免重复发送）。请手动检查一下会话。"
            ),
            failure_kind=FailureKind.TRANSIENT,
            observed_preview=last_preview,
            elapsed_seconds=elapsed,
        )

    return ConfirmOutcome(
        status=RunStatus.FAILED,
        state=ConfirmState.FAILED,
        detail=f"超时 {elapsed:.0f}s 仍未确认发送成功，且输入框内容仍在（很可能未发出）",
        failure_kind=FailureKind.TRANSIENT,
        observed_preview=last_preview,
        elapsed_seconds=elapsed,
    )


def _changed_vs_baseline(page: Any, preview: str, index: int, base: ConversationSnapshot) -> bool:
    """相对发送前的基线，页面是否出现了「确实新增了一条消息」的变化。"""
    # 1) 预览文本变了 —— 最强的证据（对消息内容无要求）
    if base.preview and preview and normalize_name(preview) != normalize_name(base.preview):
        return True

    # 2) 会话从列表中部跳到了第 1 个 —— 新消息会把会话顶到最前
    if base.index > 0 and index == 0:
        return True

    # 3) 会话里的消息条数变多了 —— 与内容无关，专治「恒定文案」
    if base.message_count >= 0:
        now = _count_messages(page)
        if now >= 0 and now > base.message_count:
            return True

    return False


# =============================================================================
# 内部
# =============================================================================


def _read_entry_for(page: Any, contact_name: str) -> Any:
    """读会话列表里某个联系人的**整行信息**（名字 / 预览 / 下标）；找不到返回 None。"""
    entries = read_conversation_list(page, limit=40)
    wanted = normalize_name(contact_name)

    # 精确匹配优先
    for entry in entries:
        if normalize_name(entry.name) == wanted:
            return entry

    # 退化到宽松匹配
    from .navigator import names_match

    for entry in entries:
        if names_match(contact_name, entry.name):
            return entry

    return None


def _read_preview_for(page: Any, contact_name: str) -> str:
    """读会话列表里某个联系人的预览文本（保留给旧调用点/测试用）。"""
    entry = _read_entry_for(page, contact_name)
    return entry.preview if entry else ""


# 数「消息区里有多少行正好等于这条消息」。
#
# 为什么用它：这是**与预览无关**、且**能抵消「本来就有一条一样的内容」**的判据 ——
# 我们比的是「发送前 vs 发送后」的差。
#
# 实测（2026-09-17，从真实会话页 dump）：消息区容器的 innerText 是
# 「刚刚\nfaze up\n昨天\n10:53\n1\n…」，每条消息**独占一行**。
# 所以按行精确比对，比在整页里做子串匹配靠谱得多。
#
# 也正因为它是按「行」比，才敢用来判断恒定文案（比如天天发「1」）：
# 发送前 1 行、发送后 2 行 → 确实新增了一条。
_COUNT_MESSAGE_LINES_JS = """
(text) => {
  const boxes = Array.from(document.querySelectorAll('[class*="MessageList"], [data-e2e="message-list"]'))
    .filter((el) => (el.innerText || '').trim().length > 0);
  if (!boxes.length) return -1;

  // 取文本最多的那个（页面上可能出现多个同名前缀的容器）
  let best = boxes[0];
  let bestLen = -1;
  for (const box of boxes) {
    const len = (box.innerText || '').length;
    if (len > bestLen) { bestLen = len; best = box; }
  }

  const norm = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
  const want = norm(text);
  if (!want) return -1;

  let count = 0;
  for (const line of (best.innerText || '').split('\\n')) {
    if (norm(line) === want) count += 1;
  }
  return count;
}
"""


def _count_message_lines(page: Any, text: str) -> int:
    """消息区里「正好是这条内容」的行数；拿不到返回 ``-1``。"""
    if not text:
        return -1
    try:
        value = page.evaluate(_COUNT_MESSAGE_LINES_JS, text)
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("读取消息区失败：%s", exc)
        return -1
    return int(value) if isinstance(value, (int, float)) else -1


def _count_messages(page: Any) -> int:
    """尽力数出当前会话里的消息条数；拿不到返回 ``-1``。

    ⚠️ **只用于前后对比**，不做绝对值判断。选择器没命中时返回 -1，
    调用方会自然地「少一个判据」，而不是拿错误的数字当真。
    """
    try:
        scope = sel.first_present(page, sel.MESSAGE_LIST) or page
    except Exception:  # noqa: BLE001
        scope = page

    for candidate in sel.MESSAGE_ITEM_CANDIDATES:
        try:
            total = scope.locator(candidate).count()
        except Exception:  # noqa: BLE001 —— 单个候选失配换下一个
            continue
        if total > 0:
            return int(total)
    return -1


def _preview_matches(preview: str, expected: str) -> bool:
    """判断预览文本是否就是我们刚发的内容。

    不做完全相等，因为：
    - 长消息在列表里会被截断
    - 表情/图片在列表里显示成 ``[表情]`` ``[图片]`` 之类的占位
    - 可能带「我：」这类前缀
    """
    p = normalize_name(preview)
    e = normalize_name(expected)

    if not p or not e:
        return False

    # 去掉可能的发送者前缀
    for prefix in ("我：", "我:", "你：", "你:"):
        if p.startswith(prefix):
            p = p[len(prefix) :].strip()
            break

    if p == e:
        return True

    # 占位文本（表情、图片、视频等）。抖音在列表里会把非文本消息渲染成
    # 方括号占位，而具体的占位措辞不稳定（同一个表情可能显示成
    # [表情] 或 [微笑]）。
    #
    # ⚠️ 这里有一个**无法从预览文本本身消除**的歧义：如果我们发的是表情，
    # 而好友恰好在我们之后发了一条「[明天见]」这样的纯文本，预览看起来
    # 一模一样。所以：
    #
    #   - 只有在我们发的确实是占位类型（expected 是 [xxx] 形式）时才走这条分支
    #   - 且要求预览也是方括号形式的占位
    #   - 并且接受「可能是好友发的」这一残留风险
    #
    # 为什么接受这个风险：误判的后果是「以为发出去了但实际没有」，
    # 而对方紧接着发来一条方括号文本的概率极低；相比之下，把表情判为
    # 发送失败然后重发一次，是更常见的损失。
    # 减少风险的办法是尽量用文本消息而不是表情——README 的 FAQ 里提到了。
    if e.startswith("[") and e.endswith("]"):
        return p.startswith("[") and p.endswith("]")

    # 截断：预览以期望内容的开头起始就算匹配
    # 取足够长的前缀，避免「在」「好」这种短消息误判
    if len(e) <= 4:
        return p == e

    probe = e[: min(len(e), 8)]
    return p.startswith(probe)


def _is_input_cleared(page: Any) -> bool:
    """输入框是否已清空。"""
    from .composer import read_input_text

    try:
        return read_input_text(page) == ""
    except Exception:  # noqa: BLE001
        return False


def _has_failure_marker(page: Any) -> bool:
    """是否出现**发送失败**标记。

    两道防线，都是为了不误判：
    1. 选择器本身（``sel.MESSAGE_FAILED``）只留精确的「发送失败」标记，
       不带 ``[class*="error"]`` 这类全页兜底；
    2. 尽量把查找范围缩到消息区，而不是整页。

    误判成失败的后果是**重试 → 重复发送**，比漏判（只报"未能确认"）严重得多。
    """
    scope = sel.first_present(page, sel.MESSAGE_LIST)
    target = scope if scope is not None else page

    for selector in sel.MESSAGE_FAILED:
        try:
            if target.locator(selector).count() > 0:
                return True
        except Exception:  # noqa: BLE001
            continue
    return False
