"""登录页「远程操作」中继的后端逻辑测试。

对应三条真实反馈（都在这里用替身固化，防止回归）：

1. **「能点但输不进验证码」** —— ``page.keyboard.type()`` 只发给**当前焦点元素**；
   没有焦点时它静默什么都不做（不抛异常）。
2. **「输入了但没用」** —— 更隐蔽的一种：文字写进了**别的框**。
   抖音登录页上同时存在「+86 国家码」「手机号」「验证码」三个可见输入框，
   而验证码框排在最后；如果总挑「第一个」，验证码就被写进了国家码框。
3. **「点了没反应」** —— 前端把用户操作误当自动刷新给「跳过」了。
   这条守在前端（app.js 的串行队列）与后端契约上。
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from douyin_huohua_keeper.workbench.routes.accounts import (
    QrActRequest,
    _apply_action,
)

CODE_INDEX = 2  # 页面上第 3 个可见输入框 = 验证码（0: +86, 1: 手机号, 2: 验证码）


class _Locator:
    def __init__(self, page: _FakePage, selector: str) -> None:
        self.page = page
        self.selector = selector

    @property
    def first(self) -> _Locator:
        return self

    def count(self) -> int:
        return 1 if self.selector == '[data-huohua-target="1"]' else 0

    def is_visible(self) -> bool:
        return True

    def click(self, timeout: Any = None) -> None:
        self.page.clicks.append(self.selector)

    def fill(self, text: str) -> None:
        if self.page.write_fails:
            return
        self.page.field_value = text

    def input_value(self) -> str:
        return self.page.field_value

    def inner_text(self) -> str:
        return self.page.field_value


class _Mouse:
    def __init__(self, page: _FakePage) -> None:
        self.page = page

    def click(self, x: float, y: float) -> None:
        self.page.mouse_clicks.append((x, y))
        if self.page.click_focuses_input:
            self.page.active_is_editable = True


class _Keyboard:
    def __init__(self, page: _FakePage) -> None:
        self.page = page

    def type(self, text: str, delay: int = 0) -> None:
        # ⚠️ 真实 Playwright：没有焦点 → 什么都不做，也不报错
        if self.page.active_is_editable and not self.page.write_fails:
            self.page.field_value = text

    def insert_text(self, text: str) -> None:
        if self.page.active_is_editable and not self.page.write_fails:
            self.page.field_value = text

    def press(self, key: str) -> None:
        self.page.pressed.append(key)


class _FakePage:
    """够用的页面替身：三个可见输入框，其中第 3 个是验证码框。"""

    def __init__(
        self,
        *,
        active_is_editable: bool = False,
        click_focuses_input: bool = True,
        write_fails: bool = False,
    ) -> None:
        self.active_is_editable = active_is_editable
        self.click_focuses_input = click_focuses_input
        self.write_fails = write_fails
        self.field_value = ""
        self.mouse_clicks: list[tuple[float, float]] = []
        self.pressed: list[str] = []
        self.clicks: list[str] = []
        self.waits: list[int] = []
        self.mouse = _Mouse(self)
        self.keyboard = _Keyboard(self)

    def locator(self, selector: str) -> _Locator:
        return _Locator(self, selector)

    def evaluate(self, script: str, arg: Any = None) -> Any:
        if "activeElement" in script:
            return {
                "tag": "input",
                "editable": self.active_is_editable,
                "type": "text",
                "placeholder": "验证码",
            }
        if "setAttribute" in script:
            # _MARK_INPUT_JS：给第 arg 个框打标记
            return {"index": arg, "placeholder": "请输入验证码", "value": self.field_value}
        if "slice(0, 8)" in script:
            # _READ_INPUTS_JS
            return [
                {"type": "text", "placeholder": "", "value": "+86"},
                {"type": "tel", "placeholder": "请输入手机号", "value": ""},
                {"type": "tel", "placeholder": "请输入验证码", "value": self.field_value},
            ]
        # 剩下的就是 _PICK_CODE_INPUT_JS：自动挑出「最像验证码」的框
        return CODE_INDEX

    def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(ms)


# =============================================================================
# 输入：必须写进**指定的**那个框，并且回读确认
# =============================================================================


class TestTypingTargetsTheRightField:
    def test_types_into_selected_field(self) -> None:
        page = _FakePage()

        result = _apply_action(
            page, QrActRequest(action="type", text="123456", index=CODE_INDEX), last_click=None
        )

        assert page.field_value == "123456"
        assert result["verified"] is True
        assert result["actual"] == "123456"
        assert "请输入验证码" in result["detail"]

    def test_auto_picks_code_field_when_index_absent(self) -> None:
        """不给序号时，不能盲选「第一个可见输入框」（那是 +86 国家码）。"""
        page = _FakePage()

        result = _apply_action(page, QrActRequest(action="type", text="8888"), last_click=None)

        assert page.field_value == "8888"
        assert result["verified"] is True
        assert result["target"]["index"] == CODE_INDEX

    def test_reports_failure_instead_of_lying(self) -> None:
        """三种写值方式都失败时，必须**明确报失败**，不能谎报「已输入」。

        背景：以前调完 `keyboard.type` 就直接回「已输入」，
        而它可能什么都没做 —— 用户看到「提示输入了、页面上却什么都没有」。
        """
        page = _FakePage(write_fails=True)

        result = _apply_action(
            page, QrActRequest(action="type", text="4321", index=CODE_INDEX), last_click=None
        )

        assert result["verified"] is False
        assert "没能写进去" in result["detail"]

    def test_type_requires_text(self) -> None:
        with pytest.raises(ValueError, match="text"):
            _apply_action(_FakePage(), QrActRequest(action="type", text=""), last_click=None)


# =============================================================================
# 聚焦 / 清空
# =============================================================================


class TestFocusAndClear:
    def test_focus_targets_the_field(self) -> None:
        page = _FakePage()

        result = _apply_action(page, QrActRequest(action="focus", index=CODE_INDEX), last_click=None)

        assert '[data-huohua-target="1"]' in page.clicks
        assert "请输入验证码" in result["detail"]

    def test_focus_autopicks_when_index_absent(self) -> None:
        page = _FakePage()

        _apply_action(page, QrActRequest(action="focus"), last_click=None)

        assert page.clicks.count('[data-huohua-target="1"]') == 1

    def test_clear_empties_the_field(self) -> None:
        page = _FakePage()
        page.field_value = "999"

        _apply_action(page, QrActRequest(action="clear", index=CODE_INDEX), last_click=None)

        assert page.field_value == ""


# =============================================================================
# 点击记忆 / 其它
# =============================================================================


class TestClickMemoryAndMisc:
    def test_click_on_input_is_remembered(self) -> None:
        page = _FakePage(click_focuses_input=True)

        result = _apply_action(page, QrActRequest(action="click", x=100, y=200), last_click=None)

        assert result["last_click"] == [100, 200]

    def test_click_on_button_is_not_remembered(self) -> None:
        """点到「获取验证码」按钮时不能记住坐标，否则输入时会重复点它（重复发短信）。"""
        page = _FakePage(click_focuses_input=False)

        result = _apply_action(page, QrActRequest(action="click", x=1, y=2), last_click=[7, 8])

        assert result["last_click"] is None

    def test_click_requires_coordinates(self) -> None:
        with pytest.raises(ValueError, match="坐标"):
            _apply_action(_FakePage(), QrActRequest(action="click"), last_click=None)

    def test_key_action(self) -> None:
        page = _FakePage()

        result = _apply_action(page, QrActRequest(action="key", key="Enter"), last_click=None)

        assert page.pressed == ["Enter"]
        assert result["detail"]

    def test_reload_resets_last_click(self) -> None:
        page = _FakePage()

        result = _apply_action(page, QrActRequest(action="reload"), last_click=[1, 1])

        assert result["last_click"] is None

    def test_unknown_action_rejected(self) -> None:
        payload = types.SimpleNamespace(action="bogus", x=None, y=None, text=None, key=None)

        with pytest.raises(ValueError, match="未知操作"):
            _apply_action(_FakePage(), payload, last_click=None)
