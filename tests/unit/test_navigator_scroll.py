"""会话定位：必须在**虚拟列表里滚着找**。

回归背景（实测事故，4/15 个好友没收到消息）：

    抖音的会话列表是**虚拟列表** —— DOM 里永远只有十几行。
    原来的查找只读一次「前 60 条」（实际就是当前这一屏），
    而每发完一个人，那个人会被顶到最上面，**把还没轮到的人越挤越往下**，
    很快挤出屏幕 → 报「找不到联系人」（kind=permanent，不会重试）。

    于是出现「一部分人收到了、另一部分人一条都没收到」，
    而日志里写的是「找不到联系人」—— 用户完全看不出为什么。

这里用替身把虚拟列表的行为固化下来：
- 只读一次只有首屏；
- 必须能**滚着找到**下面的人；
- 返回的下标必须是**当前这一屏**里的下标（否则点不到或点错人）；
- 找之前必须**先滚回顶部**（上一位联系人留下的滚动位置会让人找不到）。
"""

from __future__ import annotations

import types
from typing import Any

from douyin_huohua_keeper.engine import navigator


class _TextLoc:
    """最小文本定位器（``_extract_name`` / ``_extract_preview`` 会用到）。"""

    def __init__(self, text: str) -> None:
        self._text = text

    @property
    def first(self) -> _TextLoc:
        return self

    def count(self) -> int:
        return 1 if self._text else 0

    def nth(self, index: int) -> _TextLoc:
        return self

    def inner_text(self, **kw: Any) -> str:
        return self._text

    def get_attribute(self, name: str) -> str | None:
        return None


class _Row:
    def __init__(self, name: str, preview: str = "旧消息") -> None:
        self.name = name
        self.preview = preview

    def inner_text(self) -> str:
        return f"{self.name}\n{self.preview}\n10:19"

    def locator(self, selector: str) -> _TextLoc:
        # 名字元素：只有 title 类候选命中
        if "title" in selector.lower() or "ConversationItemtitle" in selector:
            return _TextLoc(self.name)
        return _TextLoc("")


class _RowProbe:
    def __init__(self, page: _Page, row: _Row) -> None:
        self.page = page
        self.row = row

    @property
    def first(self) -> _RowProbe:
        return self

    def count(self) -> int:
        return 1

    def nth(self, index: int) -> _RowProbe:
        return self

    def inner_text(self, **kw: Any) -> str:
        return self.row.inner_text()

    def locator(self, selector: str) -> _TextLoc:
        return self.row.locator(selector)

    def evaluate(self, script: str) -> Any:
        if "scrollTop = 0" in script:  # _SCROLL_TOP_JS
            self.page.scroll_top()
            return True
        return self.page.scroll_down()  # _SCROLL_STEP_JS


class _Probe:
    def __init__(self, page: _Page, rows: list[_Row]) -> None:
        self.page = page
        self._rows = rows

    @property
    def first(self) -> _Probe:
        return _Probe(self.page, self._rows[:1])

    def count(self) -> int:
        return len(self._rows)

    def nth(self, index: int) -> _RowProbe:
        return _RowProbe(self.page, self._rows[index])

    def locator(self, selector: str) -> _Probe:
        return _Probe(self.page, [])


class _Page:
    """模拟虚拟列表：DOM 里永远只有 ``viewport`` 行，滚动时换内容。"""

    def __init__(self, names: list[str], *, viewport: int = 3) -> None:
        self.all_rows = [_Row(n) for n in names]
        self.viewport = viewport
        self.start = 0
        self.mouse = types.SimpleNamespace(wheel=lambda *a: None)

    @property
    def visible(self) -> list[_Row]:
        return self.all_rows[self.start : self.start + self.viewport]

    def scroll_top(self) -> None:
        self.start = 0

    def scroll_down(self) -> dict[str, int]:
        before = self.start
        last = max(0, len(self.all_rows) - self.viewport)
        self.start = min(self.start + self.viewport, last)
        return {"before": before, "after": self.start, "scrollHeight": len(self.all_rows)}

    def locator(self, selector: str) -> _Probe:
        if selector == '[data-e2e="conversation-item"]':
            return _Probe(self, self.visible)
        return _Probe(self, [])

    def wait_for_timeout(self, ms: int) -> None:
        pass


# =============================================================================
# 核心：滚着找
# =============================================================================


class TestScrollFind:
    def test_finds_target_beyond_first_screen(self) -> None:
        """★ 核心回归：目标在首屏之外，必须滚下去找到它。"""
        page = _Page(["甲", "乙", "丙", "阿强", "戊"], viewport=2)

        entry = navigator._scroll_find(page, lambda name: name == "阿强")

        assert entry is not None
        assert entry.name == "阿强"

    def test_returned_index_is_within_current_screen(self) -> None:
        """返回的下标必须是**当前这一屏**里的下标 —— 否则会点错行。

        这是最危险的一类 bug：下标是「整个列表的下标」时，
        点下去可能打开别人的会话 → 发错人。
        """
        page = _Page(["甲", "乙", "丙", "阿强", "戊"], viewport=2)

        entry = navigator._scroll_find(page, lambda name: name == "阿强")

        assert entry is not None
        assert 0 <= entry.index < page.viewport
        assert page.visible[entry.index].name == "阿强"

    def test_scrolls_back_to_top_first(self) -> None:
        """列表残留在上一位联系人的位置时，也要能找到**顶部**的人。"""
        page = _Page(["甲", "乙", "丙", "丁", "戊"], viewport=2)
        page.start = 3  # 模拟上一次滚动留下的位置

        entry = navigator._scroll_find(page, lambda name: name == "甲")

        assert entry is not None
        assert entry.name == "甲"

    def test_returns_none_when_absent(self) -> None:
        page = _Page(["甲", "乙", "丙"], viewport=2)

        assert navigator._scroll_find(page, lambda name: name == "不存在") is None

    def test_finds_last_row_of_long_list(self) -> None:
        """长列表最底部的人也要能找到（用户那 4 个失败都在下面）。"""
        names = [f"好友{i}" for i in range(1, 21)] + ["阿强"]
        page = _Page(names, viewport=3)

        entry = navigator._scroll_find(page, lambda name: name == "阿强")

        assert entry is not None
        assert page.visible[entry.index].name == "阿强"


class TestLocateInList:
    def test_locates_deep_target(self) -> None:
        page = _Page(["甲", "乙", "丙", "丁", "阿珍", "己"], viewport=2)

        entry = navigator._locate_in_list(page, "阿珍")

        assert entry is not None
        assert entry.name == "阿珍"

    def test_exact_match_preferred(self) -> None:
        """「张三」不能被排在前面的「张三丰」抢走。"""
        page = _Page(["张三丰", "张三"], viewport=3)

        entry = navigator._locate_in_list(page, "张三")

        assert entry is not None
        assert entry.name == "张三"


# =============================================================================
# 点击前核对（防止点错行 → 发错人）
# =============================================================================


class TestRowVerification:
    def test_row_matches(self) -> None:
        page = _Page(["甲", "乙"], viewport=2)
        probe = page.locator('[data-e2e="conversation-item"]')

        assert navigator._row_matches(probe, 0, "甲") is True
        assert navigator._row_matches(probe, 0, "乙") is False

    def test_find_index_in_scope(self) -> None:
        page = _Page(["甲", "乙", "丙"], viewport=3)
        probe = page.locator('[data-e2e="conversation-item"]')

        assert navigator._find_index_in_scope(probe, "丙") == 2
        assert navigator._find_index_in_scope(probe, "不存在") == -1
