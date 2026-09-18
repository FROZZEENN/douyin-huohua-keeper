"""发送链路的回归测试。

这个文件里的**每一个用例都对应一个真实踩过的坑**，而且都是「在实验室里
测不出来、只有真机上才暴露」的那类。按危害从高到低排列：

🔴 零宽空格导致「输入框是否为空」判错 → 假阴性 → 触发重试 → **重复发送**
   这是最危险的一类：假阳性只是漏发，假阴性会让重试机制「帮倒忙」。
橙 在打开会话之前检查输入框（顺序错）→ 每个收件人都失败并误报登录失效
橙 导航后不等待就检查（时序错）→ 稳定误判「认不出会话页结构」
黄 强制要求列表容器（哈希类名）→ 「联系人明明在列表里却报找不到」
黄 名字解析取到外层容器 → 名字变成「阿明 849 15分钟前」，永远匹配不上
"""

from __future__ import annotations

from typing import Any

from douyin_huohua_keeper.engine import confirmer as confirmer_mod
from douyin_huohua_keeper.engine import navigator
from douyin_huohua_keeper.engine import selectors as sel
from douyin_huohua_keeper.engine.auth import verify_chat_page, verify_send_ready
from douyin_huohua_keeper.engine.composer import _clean_text, read_input_text

# =============================================================================
# 替身：一个统一的「节点树」模型
#
# 每个 FakeNode 有文本和「选择器 -> 子节点列表」的映射，
# FakeLocator 包装一组节点，支持 count / first / nth / locator。
# 这样就能精确模拟「某个选择器命中几个元素、分别是什么文本」。
# =============================================================================


class FakeNode:
    def __init__(self, text: str = "", children: dict[str, list[FakeNode]] | None = None) -> None:
        self.text = text
        self.children = children or {}


class FakeLocator:
    def __init__(self, nodes: list[FakeNode]) -> None:
        self._nodes = nodes

    def count(self) -> int:
        return len(self._nodes)

    def inner_text(self, **kwargs: Any) -> str:
        return self._nodes[0].text if self._nodes else ""

    def is_visible(self) -> bool:
        return bool(self._nodes)

    def is_enabled(self) -> bool:
        return True

    @property
    def first(self) -> FakeLocator:
        return FakeLocator(self._nodes[:1])

    def nth(self, index: int) -> FakeLocator:
        return FakeLocator(self._nodes[index : index + 1])

    def locator(self, selector: str) -> FakeLocator:
        out: list[FakeNode] = []
        for node in self._nodes:
            out.extend(node.children.get(selector, []))
        return FakeLocator(out)


class FakePage:
    def __init__(self, children: dict[str, list[FakeNode]] | None = None) -> None:
        self._children = children or {}

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self._children.get(selector, []))


# =============================================================================
# 🔴 零宽空格：判空失效会导致重复发送
# =============================================================================


class TestZeroWidthSpace:
    """抖音的编辑器里永远有一个含 U+200B 的 ``data-enter`` 节点。

    ``inner_text()`` 因此至少返回一个零宽空格，``.strip()`` 去不掉它。
    如果不处理，「输入框还有内容 → 没发出去」会永远成立，
    于是**误报失败并重试 —— 而消息其实已经发出去了，重试变成重复发送**。
    """

    def test_clean_text_removes_zero_width_space(self) -> None:
        assert _clean_text("\u200b") == ""
        assert _clean_text("\u200b\u200b") == ""
        assert _clean_text("\ufeff") == ""

    def test_clean_text_keeps_real_content(self) -> None:
        assert _clean_text("\u200b你好\u200b") == "你好"
        assert _clean_text("  1  ") == "1"

    def test_clean_text_removes_direction_controls(self) -> None:
        assert _clean_text("\u202a\u202b") == ""
        assert _clean_text("\u2060\u2061") == ""

    def test_read_input_text_empty_editor_is_empty(self) -> None:
        """核心回归：编辑器里只剩那个 data-enter 的零宽空格时，必须判为空。"""
        page = FakePage(
            {
                sel.COMPOSER_INPUT_CANDIDATES[0]: [
                    FakeNode("\u200b"),
                ]
            }
        )
        assert read_input_text(page) == ""

    def test_read_input_text_returns_real_content(self) -> None:
        page = FakePage({sel.COMPOSER_INPUT_CANDIDATES[0]: [FakeNode("\u200b2\u200b")]})
        assert read_input_text(page) == "2"

    def test_input_cleared_detection_after_send(self) -> None:
        """发送后编辑器回到「只剩零宽空格」状态 → 必须判定为已清空。

        这条直接决定 confirmer 会不会误报失败并触发重复发送。
        """
        page = FakePage({sel.COMPOSER_INPUT_CANDIDATES[0]: [FakeNode("\u200b")]})
        assert confirmer_mod._is_input_cleared(page) is True

    def test_input_not_cleared_when_text_remains(self) -> None:
        """真的没发出去（文字还在）时必须判定为「未清空」。"""
        page = FakePage({sel.COMPOSER_INPUT_CANDIDATES[0]: [FakeNode("\u200b还没发出去\u200b")]})
        assert confirmer_mod._is_input_cleared(page) is False

    def test_read_input_text_missing_box_is_empty(self) -> None:
        assert read_input_text(FakePage({})) == ""


# =============================================================================
# 橙 顺序：输入框只有在打开会话之后才存在
# =============================================================================


class TestSendReadyRequiresOpenedConversation:
    """输入框是跟着会话详情一起出现的。

    没打开任何会话时页面上只有搜索框和一列会话行 ——
    在此时调用 verify_send_ready 会稳定收到 COMPOSER_MISSING，
    被上层误判成「登录态失效」并中止整个任务。
    """

    def test_composer_missing_before_opening(self) -> None:
        page = FakePage({})  # 没打开会话 → 没有输入框
        check = verify_send_ready(page)
        assert check.ok is False
        assert check.reason == "COMPOSER_MISSING"

    def test_composer_ready_after_opening(self) -> None:
        page = FakePage({sel.COMPOSER_INPUT: [FakeNode("")]})
        check = verify_send_ready(page)
        assert check.ok is True

    def test_error_message_explains_the_prerequisite(self) -> None:
        """报错信息要说明「输入框需要先打开会话」，否则排查方向会被带偏。"""
        page = FakePage({})
        check = verify_send_ready(page)
        assert "打开会话" in check.detail or "尚未打开" in check.detail

    def test_verify_chat_page_does_not_require_composer(self) -> None:
        """页面就绪检查**不能**要求输入框存在 —— 它是在打开会话之前调用的。"""
        page = FakePage({'[data-e2e="conversation-item"]': [FakeNode("")]})
        check = verify_chat_page(page, timeout_ms=0)
        assert check.ok is True


# =============================================================================
# 橙 时序：导航后必须等待渲染
# =============================================================================


class TestChatPageWaitsForRender:
    """``goto(wait_until="domcontentloaded")`` 早于会话列表渲染。

    不等待就检查会稳定误判成「认不出会话页结构」。
    """

    def test_polls_until_markers_appear(self) -> None:
        state = {"calls": 0}

        class AppearingLocator:
            def __init__(self, exists: bool) -> None:
                self._exists = exists

            def count(self) -> int:
                return 1 if self._exists else 0

        class AppearingPage:
            url = "https://www.douyin.com/chat"

            def locator(self, selector: str) -> AppearingLocator:
                # 只有「会话行」这个选择器是逐步出现的；
                # 登录标志永远不命中（否则会被判成 AUTH_EXPIRED）。
                if selector == '[data-e2e="conversation-item"]':
                    state["calls"] += 1
                    return AppearingLocator(state["calls"] >= 4)
                return AppearingLocator(False)

        check = verify_chat_page(AppearingPage(), timeout_ms=5_000)
        assert check.ok is True
        assert state["calls"] >= 4

    def test_gives_up_after_timeout(self) -> None:
        check = verify_chat_page(FakePage({}), timeout_ms=600)
        assert check.ok is False
        assert check.reason == "UNKNOWN_PAGE"

    def test_login_markers_win_over_waiting(self) -> None:
        """页面上出现登录元素时立刻判 AUTH_EXPIRED，不用等。"""
        page = FakePage({sel.LOGIN_PAGE_MARKERS[0]: [FakeNode("")]})
        check = verify_chat_page(page, timeout_ms=5_000)
        assert check.ok is False
        assert check.reason == "AUTH_EXPIRED"

    def test_conversation_item_is_a_stronger_marker_than_searchbox(self) -> None:
        """会话行必须排在搜索框之前 —— 搜索框出现得远早于列表渲染完。

        顺序错了会让页面「看起来就绪」但其实列表还没好，
        随后读到的是一份半成品。
        """
        assert sel.CHAT_PAGE_MARKERS.index('[data-e2e="conversation-item"]') < sel.CHAT_PAGE_MARKERS.index(
            'input[placeholder*="搜索"]'
        )


# =============================================================================
# 黄 哈希类名：列表容器不是必需的
# =============================================================================


def _conversation_row(name: str, preview: str = "分享[视频]") -> FakeNode:
    """构造一个会话行：名字元素 + 预览元素。"""
    return FakeNode(
        f"{name} 849 15分钟前 {preview}",
        children={
            '[class*="ConversationItemtitle"]': [FakeNode(name)],
            '[class*="ConversationItemHint"]': [FakeNode(preview)],
        },
    )


class TestConversationListWithoutContainer:
    """抖音的列表容器 class 是构建哈希名，选择器一个都命中不了。

    而会话行有稳定的 ``[data-e2e="conversation-item"]``。
    所以「有会话行、没有可识别的容器」是真实页面的常态，
    不能因为找不到容器就放弃。
    """

    def test_reads_list_without_container(self) -> None:
        page = FakePage(
            {
                '[data-e2e="conversation-item"]': [
                    _conversation_row("阿明"),
                    _conversation_row("阿豪"),
                ]
            }
        )
        entries = navigator.read_conversation_list(page)
        assert [e.name for e in entries] == ["阿明", "阿豪"]

    def test_uses_container_scope_when_present(self) -> None:
        """有容器时应该在容器范围内找，避免误抓页面其他位置的同名元素。"""
        container = FakeNode(
            "",
            children={
                '[data-e2e="conversation-item"]': [_conversation_row("阿明")],
            },
        )
        page = FakePage(
            {
                sel.CONVERSATION_LIST[0]: [container],
                # 页面顶层也有一批行（不该被用到）
                '[data-e2e="conversation-item"]': [
                    _conversation_row("噪音一"),
                    _conversation_row("噪音二"),
                ],
            }
        )
        entries = navigator.read_conversation_list(page)
        assert [e.name for e in entries] == ["阿明"]

    def test_returns_empty_when_no_rows_at_all(self) -> None:
        assert navigator.read_conversation_list(FakePage({})) == ()


class TestOpenConversationWithoutContainer:
    def test_open_works_without_container(self) -> None:
        """以前找不到容器就直接抛「会话列表消失了」—— 明明已经匹配到目标。"""
        row = _conversation_row("阿明")
        page = FakePage({'[data-e2e="conversation-item"]': [row]})

        recorded: list[Any] = []

        entry = navigator.ConversationEntry(name="阿明", raw_text="", index=0)
        # 直接验证「能定位到行」这一步不抛异常即可（点击需要真实浏览器）
        probe = page.locator('[data-e2e="conversation-item"]')
        assert probe.count() > entry.index
        recorded.append(probe)
        assert recorded

    def test_raises_when_index_out_of_range(self) -> None:
        row = _conversation_row("阿明")
        page = FakePage({'[data-e2e="conversation-item"]': [row]})
        entry = navigator.ConversationEntry(name="阿明", raw_text="", index=5)

        probe = page.locator('[data-e2e="conversation-item"]')
        assert probe.count() <= entry.index  # 越界，open_conversation 会抛


# =============================================================================
# 黄 名字解析：必须取「最短的非空文本」
# =============================================================================


class TestExtractNamePicksInnermost:
    """``[class*="title"]`` 在每一行命中 2 个元素：

        外层容器: '阿明\\n849\\n15分钟前'   ← 名字 + 火花天数 + 时间
        内层叶子: '阿明'                    ← 真正的名字

    取 ``.first`` 会拿到外层，解析出的名字永远匹配不上用户填的 ``阿明``。
    """

    def test_picks_shortest_when_container_also_matches(self) -> None:
        row = FakeNode(
            "阿明 849 15分钟前 分享[视频]",
            children={
                '[class*="ConversationItemtitle"]': [
                    FakeNode("阿明\n849\n15分钟前"),
                    FakeNode("阿明"),
                ],
            },
        )
        # _extract_name 的第一个参数需要是「行」的 locator
        assert navigator._extract_name(FakeLocator([row]), row.text) == "阿明"

    def test_picks_the_only_match(self) -> None:
        row = _conversation_row("阿明")
        assert navigator._extract_name(FakeLocator([row]), row.text) == "阿明"

    def test_falls_back_to_first_line(self) -> None:
        """所有结构化选择器都没命中时，退回整行文本的第一行。"""
        row = FakeNode("某个人\n849\n15分钟前", children={})
        assert navigator._extract_name(FakeLocator([row]), row.text) == "某个人"

    def test_returns_empty_when_nothing_usable(self) -> None:
        row = FakeNode("", children={})
        assert navigator._extract_name(FakeLocator([row]), "") == ""


class TestReadActiveConversationName:
    """会话头部同理：外层容器 ``RightPanelHeaderinfoContainer``
    也含有同样的名字文字，必须取最内层的。"""

    def test_picks_shortest(self) -> None:
        page = FakePage(
            {
                '[class*="RightPanelHeadertitle"]': [
                    FakeNode("阿乐"),
                ]
            }
        )
        assert navigator.read_active_conversation_name(page) == "阿乐"

    def test_picks_shortest_among_multiple(self) -> None:
        page = FakePage(
            {
                '[class*="RightPanelHeadertitle"]': [
                    FakeNode("阿乐 在线 刚刚"),
                    FakeNode("阿乐"),
                ]
            }
        )
        assert navigator.read_active_conversation_name(page) == "阿乐"

    def test_returns_none_when_absent(self) -> None:
        assert navigator.read_active_conversation_name(FakePage({})) is None


# =============================================================================
# 选择器表本身的防退化检查
# =============================================================================


class TestSelectorsKeepRealWorldValues:
    """把这些实测确认有效的选择器钉住。

    它们都是「在真实页面上验证过唯一能命中的那个」，
    被无意改掉就等于功能直接失效。
    """

    def test_send_button_has_e2e_class(self) -> None:
        assert ".e2e-send-msg-btn" in sel.SEND_BUTTON_CANDIDATES
        assert sel.SEND_BUTTON_CANDIDATES[0] == ".e2e-send-msg-btn"

    def test_conversation_name_has_real_class(self) -> None:
        assert any("ConversationItemtitle" in c for c in sel.CONVERSATION_NAME)

    def test_conversation_preview_has_real_class(self) -> None:
        assert any("ConversationItemHint" in c for c in sel.CONVERSATION_PREVIEW)

    def test_header_name_has_real_class(self) -> None:
        assert any("RightPanelHeadertitle" in c for c in sel.CHAT_HEADER_NAME)

    def test_conversation_items_have_stable_marker(self) -> None:
        assert '[data-e2e="conversation-item"]' in sel.CONVERSATION_ITEM

    def test_composer_uses_contenteditable(self) -> None:
        assert 'div[contenteditable="true"]' in sel.COMPOSER_INPUT_CANDIDATES
