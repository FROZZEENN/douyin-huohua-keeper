"""会话定位的纯逻辑测试。

不发网络请求、不开浏览器。测的是「怎么判断两个名字是同一个人」这件事 ——
这个判断错了会导致消息发给错误的人，是本项目里后果最严重的 bug。
"""

from __future__ import annotations

import pytest

from douyin_huohua_keeper.engine.navigator import (
    ConversationEntry,
    names_match,
    normalize_name,
    strip_group_suffix,
)


class TestNormalizeName:
    def test_strips_whitespace(self) -> None:
        assert normalize_name("  小明  ") == "小明"

    def test_collapses_internal_whitespace(self) -> None:
        assert normalize_name("小  明") == "小 明"

    def test_removes_zero_width_characters(self) -> None:
        """页面抠出来的文本经常混进零宽字符，肉眼看不见但比较会失败。"""
        assert normalize_name("小\u200b明") == "小明"
        assert normalize_name("小\ufeff明") == "小明"
        assert normalize_name("\u200e小明\u200f") == "小明"

    def test_handles_empty(self) -> None:
        assert normalize_name("") == ""

    def test_handles_none_like(self) -> None:
        assert normalize_name(None) == ""  # type: ignore[arg-type]


class TestStripGroupSuffix:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("小明（3）", "小明"),
            ("小明(3)", "小明"),
            ("小明 [5]", "小明"),
            ("小明【8】", "小明"),
            ("小明（12 人）", "小明（12 人）"),  # 带「人」字的不算后缀，保守处理
            ("小明", "小明"),
            ("（3）", "（3）"),  # 全是后缀时不剥，避免剥没了
        ],
    )
    def test_variants(self, raw: str, expected: str) -> None:
        assert strip_group_suffix(raw) == expected


class TestNamesMatch:
    def test_exact_match(self) -> None:
        assert names_match("小明", "小明")

    def test_exact_match_after_normalization(self) -> None:
        assert names_match(" 小明 ", "\u200b小明")

    def test_group_suffix_tolerated(self) -> None:
        """群名带成员数后缀是常态，要能匹配上。"""
        assert names_match("小明", "小明（3）")
        assert names_match("小明（3）", "小明")

    def test_does_not_match_different_people(self) -> None:
        """最关键的一条：张三绝不能匹配到张三丰。"""
        assert not names_match("张三", "张三丰")
        assert not names_match("张三丰", "张三")

    def test_does_not_match_prefix(self) -> None:
        assert not names_match("小", "小明")
        assert not names_match("大明", "小明")

    def test_empty_never_matches(self) -> None:
        assert not names_match("", "小明")
        assert not names_match("小明", "")
        assert not names_match("", "")

    def test_case_sensitive_for_latin_names(self) -> None:
        """英文名大小写不同可能是不同的人（比如 Alice / alice 两个账号）。"""
        assert not names_match("Alice", "alice")
        assert names_match("Alice", "Alice")

    def test_emoji_names(self) -> None:
        assert names_match("🌟小星星", "🌟小星星")
        assert not names_match("🌟小星星", "⭐小星星")


class TestConversationEntry:
    def test_normalized_accessor(self) -> None:
        entry = ConversationEntry(name="\u200b小明 ", raw_text="小明\n昨天聊的")
        assert entry.normalized() == "小明"

    def test_defaults(self) -> None:
        entry = ConversationEntry(name="小明", raw_text="小明")

        assert entry.preview == ""
        assert entry.index == -1
