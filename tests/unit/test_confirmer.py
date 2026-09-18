"""发送确认状态机的纯逻辑测试。

``_preview_matches`` 是这里最需要测透的函数：它太宽松会误报成功
（火花静默断掉没人知道），太严格会误报失败（重复发送骚扰好友）。
两边都是实际损失。
"""

from __future__ import annotations

from typing import Any

import pytest

from douyin_huohua_keeper.engine import confirmer
from douyin_huohua_keeper.engine.confirmer import (
    ConfirmOutcome,
    ConfirmState,
    _preview_matches,
)
from douyin_huohua_keeper.engine.navigator import ConversationEntry
from douyin_huohua_keeper.models import FailureKind, RunStatus


class FakeLocator:
    """最小的 locator 替身。"""

    def __init__(self, count: int = 0, text: str = "") -> None:
        self._count = count
        self._text = text

    def count(self) -> int:
        return self._count

    def first(self) -> FakeLocator:
        return self

    def inner_text(self) -> str:
        return self._text

    def is_visible(self) -> bool:
        return self._count > 0

    def nth(self, index: int) -> FakeLocator:
        return self


class FakePage:
    """极简 page 替身，只实现确认逻辑用到的方法。"""

    def __init__(self, *, failure_marker: bool = False) -> None:
        self.failure_marker = failure_marker

    def locator(self, selector: str):
        if "failed" in selector or "error" in selector:
            return FakeLocator(count=1 if self.failure_marker else 0)
        if "contenteditable" in selector:
            return FakeLocator(count=1, text="")
        return FakeLocator(count=0)


class CountedPage(FakePage):
    """让「消息条数」按序列变化的页面替身，用于测 ``message_grew`` 判据。

    只有消息气泡候选里的**第一个**选择器会返回序列值，其余返回 0 ——
    这样 ``_count_messages``（取第一个命中项）拿到的就是序列值。
    """

    def __init__(self, *, counts: list[int], **kwargs) -> None:
        super().__init__(**kwargs)
        self._counts = list(counts)
        self._seq = 0

    def locator(self, selector: str):
        from douyin_huohua_keeper.engine import selectors as sel

        if selector == sel.MESSAGE_ITEM_CANDIDATES[0]:
            if self._seq < len(self._counts):
                value = self._counts[self._seq]
            else:
                value = self._counts[-1] if self._counts else 0
            self._seq += 1
            return FakeLocator(count=value)
        if selector in sel.MESSAGE_ITEM_CANDIDATES:
            return FakeLocator(count=0)
        return super().locator(selector)


class MessageAreaPage(FakePage):
    """消息区里「这条内容」的行数按序列变化；会话列表预览**始终不匹配**。

    复现真实场景：抖音会把会话列表预览换成系统提示
    （实测：`阿伟` 发完就变成「不用等，现在去多闪互聊 火花当天重燃 去多闪」），
    于是「预览比对」永远失败 —— 但消息区里确实多了一条。
    这时必须靠消息区的行数变化来确认，否则明明发出去了却报「不确定」。
    """

    def __init__(self, *, counts: list[int], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._counts = list(counts)
        self._seq = 0

    def evaluate(self, script: str, arg: Any = None) -> Any:
        if self._seq < len(self._counts):
            value = self._counts[self._seq]
        else:
            value = self._counts[-1] if self._counts else 0
        self._seq += 1
        return value


@pytest.fixture
def fake_entries(monkeypatch: pytest.MonkeyPatch):
    """替换 read_conversation_list，让确认流程可以脱离浏览器测试。"""

    def install(entries: tuple[ConversationEntry, ...]) -> None:
        monkeypatch.setattr(confirmer, "read_conversation_list", lambda page, limit=40: entries)

    return install


class TestPreviewMatching:
    def test_exact_match(self) -> None:
        assert _preview_matches("今天天气不错", "今天天气不错")

    def test_normalizes_whitespace(self) -> None:
        assert _preview_matches("  今天天气不错  ", "今天天气不错")

    def test_strips_sender_prefix(self) -> None:
        """会话列表里自己发的消息常带「我：」前缀。"""
        assert _preview_matches("我：今天天气不错", "今天天气不错")
        assert _preview_matches("我:今天天气不错", "今天天气不错")

    def test_truncated_preview_matches(self) -> None:
        """长消息在列表里会被截断，截断部分要能匹配。"""
        long_text = "今天天气真不错啊我们出去走走吧顺便吃个饭"
        assert _preview_matches(long_text[:10], long_text)

    def test_short_message_requires_exact(self) -> None:
        """短消息必须完全相等 —— 否则「在」会匹配到「在吗」。"""
        assert _preview_matches("在", "在")
        assert not _preview_matches("在吗", "在")
        assert not _preview_matches("在", "在吗")

    def test_short_message_prefix_not_matched(self) -> None:
        """防误报的关键：发「早」，预览是「早上好呀」时必须判为不匹配。"""
        assert not _preview_matches("早上好呀", "早")

    def test_sticker_placeholder(self) -> None:
        # 抖音对非文本消息的占位措辞不稳定，[微笑] 和 [表情] 都可能是
        # 同一个表情在列表里的显示形式
        assert _preview_matches("[表情]", "[表情]")
        assert _preview_matches("[微笑]", "[表情]")

    def test_image_placeholder(self) -> None:
        assert _preview_matches("[图片]", "[图片]")

    def test_placeholder_kind_must_match(self) -> None:
        """[图片] 不能匹配到文本消息，反之亦然。"""
        assert not _preview_matches("[图片]", "今天天气不错")
        assert not _preview_matches("今天天气不错", "[图片]")

    def test_bracket_text_ambiguity_is_a_known_limitation(self) -> None:
        """已知局限：[明天见] 和 [图片] 在预览文本里长得一样。

        这个歧义无法从列表预览本身消除 —— 抖音把非文本消息渲染成方括号占位，
        而措辞不固定。这里把这个行为**显式记下来**，而不是假装它不存在。

        后果：如果好友恰好在之后发了一条方括号文本，我们可能误判为发送成功。
        概率极低（需要好友在我们发送后的几百毫秒内正好发一条 [xxx]），
        而反过来的代价（表情被判失败然后重发）更常见 —— 所以选择接受。

        缓解办法：优先用文本消息。见 README 的 FAQ。
        """
        assert _preview_matches("[明天见]", "[图片]") is True

    def test_long_bracket_text_still_matches(self) -> None:
        """占位判断不限制长度 —— 抖音的占位文本可能带表情名。"""
        assert _preview_matches("[呲牙笑]", "[表情]")

    def test_empty_never_matches(self) -> None:
        assert not _preview_matches("", "今天天气不错")
        assert not _preview_matches("今天天气不错", "")

    def test_different_content_not_matched(self) -> None:
        assert not _preview_matches("昨天聊的事情", "今天天气不错")

    def test_long_message_prefix_matched(self) -> None:
        expected = "我们明天上午十点在公司门口集合吧"
        assert _preview_matches("我们明天上午十点", expected)


class TestConfirmOutcomeSemantics:
    def test_confirmed_property(self) -> None:
        outcome = ConfirmOutcome(
            status=RunStatus.SUCCESS,
            state=ConfirmState.CONFIRMED,
            detail="ok",
        )
        assert outcome.confirmed is True

    def test_uncertain_is_not_confirmed(self) -> None:
        """UNCERTAIN 是「不知道」，绝不能当成成功。"""
        outcome = ConfirmOutcome(
            status=RunStatus.UNCERTAIN,
            state=ConfirmState.UNCERTAIN,
            detail="不确定",
        )
        assert outcome.confirmed is False

    def test_uncertain_maps_to_transient(self) -> None:
        """不确定按可重试处理，但上层必须用两阶段防重挡一下。"""
        outcome = ConfirmOutcome(
            status=RunStatus.UNCERTAIN,
            state=ConfirmState.UNCERTAIN,
            detail="不确定",
            failure_kind=FailureKind.TRANSIENT,
        )
        assert outcome.failure_kind is FailureKind.TRANSIENT


class TestConfirmationFlow:
    def test_confirms_when_preview_changed(self, fake_entries) -> None:
        """预览**由旧内容变成**期望内容时应该尽快确认，而不是傻等超时。"""
        fake_entries((ConversationEntry(name="小明", raw_text="小明", preview="今天天气不错", index=0),))

        outcome = confirmer.confirm_sent(
            FakePage(),
            contact_name="小明",
            expected_text="今天天气不错",
            timeout_ms=3_000,
            poll_interval=0.01,
            require_stable=2,
            baseline=confirmer.ConversationSnapshot(preview="昨天聊的事情", index=0),
        )

        assert outcome.state is ConfirmState.CONFIRMED
        assert outcome.status is RunStatus.SUCCESS
        assert outcome.elapsed_seconds < 2.0, "不该傻等到超时"

    def test_detects_failure_marker(self, fake_entries) -> None:
        fake_entries((ConversationEntry(name="小明", raw_text="小明", preview="旧消息", index=0),))

        outcome = confirmer.confirm_sent(
            FakePage(failure_marker=True),
            contact_name="小明",
            expected_text="新消息",
            timeout_ms=3_000,
            poll_interval=0.01,
        )

        assert outcome.state is ConfirmState.FAILED
        assert outcome.status is RunStatus.FAILED

    def test_never_matches_other_persons_preview(self, fake_entries) -> None:
        """列表里的预览属于别人时，绝不能算作成功。"""
        fake_entries((ConversationEntry(name="小红", raw_text="小红", preview="今天天气不错", index=0),))

        outcome = confirmer.confirm_sent(
            FakePage(),
            contact_name="小明",
            expected_text="今天天气不错",
            timeout_ms=400,
            poll_interval=0.01,
            require_stable=1,
        )

        assert outcome.state is not ConfirmState.CONFIRMED

    def test_requires_stable_observations(self, fake_entries) -> None:
        """单次命中不算数 —— 页面可能回滚。"""
        fake_entries((ConversationEntry(name="小明", raw_text="小明", preview="今天天气不错", index=0),))

        outcome = confirmer.confirm_sent(
            FakePage(),
            contact_name="小明",
            expected_text="今天天气不错",
            timeout_ms=3_000,
            poll_interval=0.01,
            require_stable=3,
            baseline=confirmer.ConversationSnapshot(preview="旧消息", index=0),
        )

        assert outcome.state is ConfirmState.CONFIRMED

    def test_already_identical_preview_is_never_confirmed(self, fake_entries) -> None:
        """★ 核心回归：发送前的预览**本来就等于**要发的内容。

        这是实测事故的精确复现：消息恒为「1」，昨天那条「1」还在列表里，
        而发送后页面没有任何变化（因为什么都没发出去，或者内容本来就一样）。
        旧实现只比对「预览 == 期望内容」，于是每次都在 1 秒内判成功 ——
        工作台显示「今天已发送」，对方却什么都没收到。

        正确行为：判 **UNCERTAIN**（不知道发出去没有），绝不判成功。
        """
        fake_entries((ConversationEntry(name="小明", raw_text="小明", preview="1", index=0),))

        outcome = confirmer.confirm_sent(
            FakePage(),
            contact_name="小明",
            expected_text="1",
            timeout_ms=400,
            poll_interval=0.01,
            require_stable=1,
            baseline=confirmer.ConversationSnapshot(preview="1", index=0),
        )

        assert outcome.state is ConfirmState.UNCERTAIN
        assert outcome.status is RunStatus.UNCERTAIN
        assert "无法确认" in outcome.detail

    def test_confirms_when_conversation_moved_to_top(self, fake_entries) -> None:
        """预览没变、但会话从列表中部跳到了第一位 —— 说明确实来了新消息。

        这是恒定文案场景下的救命判据：新消息会把会话顶到列表最前。
        """
        fake_entries((ConversationEntry(name="小明", raw_text="小明", preview="1", index=0),))

        outcome = confirmer.confirm_sent(
            FakePage(),
            contact_name="小明",
            expected_text="1",
            timeout_ms=3_000,
            poll_interval=0.01,
            require_stable=1,
            baseline=confirmer.ConversationSnapshot(preview="1", index=5),
        )

        assert outcome.state is ConfirmState.CONFIRMED

    def test_confirms_when_message_count_grew(self, fake_entries) -> None:
        """会话里的消息条数变多 = 确实新增了一条（与内容无关）。"""
        fake_entries((ConversationEntry(name="小明", raw_text="小明", preview="1", index=0),))

        page = CountedPage(counts=[4, 4, 5, 5, 5])

        outcome = confirmer.confirm_sent(
            page,
            contact_name="小明",
            expected_text="1",
            timeout_ms=3_000,
            poll_interval=0.01,
            require_stable=1,
            baseline=confirmer.ConversationSnapshot(preview="1", index=0, message_count=4),
        )

        assert outcome.state is ConfirmState.CONFIRMED

    def test_confirms_when_message_area_gained_a_line(self, fake_entries) -> None:
        """★ 核心回归：会话列表预览被系统提示顶掉、比对不了，

        但**消息区里确实多了一条** —— 这种情况必须能确认成功。

        实测事故：4 个位置较深的会话补发后全部误报「结果不确定」，
        而它们其实都到了（消息区里明明有「刚刚 faze up」）。
        """
        fake_entries(
            (
                ConversationEntry(
                    name="小明",
                    raw_text="小明",
                    preview="不用等，现在去多闪互聊 火花当天重燃",
                    index=0,
                ),
            )
        )
        page = MessageAreaPage(counts=[1, 1, 2, 2, 2])

        outcome = confirmer.confirm_sent(
            page,
            contact_name="小明",
            expected_text="faze up",
            timeout_ms=3_000,
            poll_interval=0.01,
            require_stable=1,
            baseline=confirmer.ConversationSnapshot(
                preview="不用等，现在去多闪互聊 火花当天重燃", index=0, needle_count=1
            ),
        )

        assert outcome.state is ConfirmState.CONFIRMED
        assert outcome.status is RunStatus.SUCCESS
        assert "新增" in outcome.detail

    def test_message_area_unavailable_degrades_gracefully(self) -> None:
        """读不到消息区时返回 -1 —— 只是少一个判据，不会误报。"""
        assert confirmer._count_message_lines(FakePage(), "faze up") == -1

    def test_empty_list_does_not_confirm(self, fake_entries) -> None:
        fake_entries(())

        outcome = confirmer.confirm_sent(
            FakePage(),
            contact_name="小明",
            expected_text="今天天气不错",
            timeout_ms=300,
            poll_interval=0.01,
        )

        assert outcome.state in {ConfirmState.FAILED, ConfirmState.UNCERTAIN}
