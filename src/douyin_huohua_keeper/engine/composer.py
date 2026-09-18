"""消息构建与输入。

三件事：把 :class:`Message` 变成输入框里的内容、发送、随机挑一条消息。

关于「输入节奏模拟」：这个项目**不做打字延迟模拟**。
理由不是偷懒，而是：

1. 用 ``insert_text`` 一次性插入和逐字敲在 DOM 层面产生的事件序列不同，
   前者更可控。逐字模拟一旦中断就会留下半截内容，反而增加出错概率。
2. 页面上的行为特征主要来自「什么时候发、发给谁、发什么」，
   而不是「每个字符间隔多少毫秒」。前者才值得花心思。
3. 更重要的是：**任何行为模拟都不能规避平台风控**，写进代码里只会给人
   虚假的安全感。这一点在 README 和 SECURITY 里都写明了。
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import Message
from . import selectors as sel

LOGGER = logging.getLogger(__name__)

# 零宽字符与方向控制符。
#
# 这些字符**肉眼看不见**，但会让「输入框是不是空的」这个判断失效。
# 具体到抖音的编辑器（实测量到的 DOM）：
#
#     <div class="ace-line" data-node="true">
#       <span data-string="true" data-leaf="true">正文</span>
#       <span data-enter="true"  data-leaf="true">​</span>   ← U+200B 零宽空格
#     </div>
#
# **末尾那个 data-enter 的 span 永远存在**，所以 ``inner_text()`` 在
# 输入框为空时也会返回一个 ``\\u200b``，而 ``.strip()`` 去不掉它。
#
# 后果非常严重：发送确认会认为「输入框里还有内容 → 没发出去」，
# 于是**误报失败并触发重试** —— 可消息其实已经发出去了，
# 重试就变成了**重复发送**。实测一次任务因此重复发了多条消息。
_INVISIBLE_CHARS = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")


def _clean_text(raw: str) -> str:
    """去掉零宽字符，再合并空白、去首尾。"""
    return re.sub(r"\s+", " ", _INVISIBLE_CHARS.sub("", raw or "")).strip()


class ComposeError(RuntimeError):
    """消息无法输入。"""


class StickerNotFoundError(ComposeError):
    """找不到指定的表情。"""


@dataclass(frozen=True, slots=True)
class ComposeResult:
    """输入结果。记录了「实际输入了什么」，供确认状态机比对。"""

    kind: str
    text: str = ""
    detail: str = ""


def pick_message(messages: tuple[Message, ...], *, rng: random.Random | None = None) -> Message:
    """从消息池里挑一条。

    ``random`` 类型的消息会被展开一层（它的 choices 里随机选一个）。
    这是「看起来不那么机械」的主要手段 —— 连续 700 天发同一个字，
    肉眼都能看出是机器。

    传入 ``rng`` 是为了让测试可复现。
    """
    if not messages:
        raise ValueError("消息池为空，无法挑选")

    chooser = rng or random
    message = chooser.choice(messages)

    if message.kind == "random":
        if not message.choices:
            raise ValueError("random 消息没有候选，配置有误")
        return chooser.choice(message.choices)

    return message


def compose(page: Any, message: Message, *, dry_run: bool = False) -> ComposeResult:
    """把消息输入到输入框。**不发送** —— 发送由 caller 决定时机。

    拆开输入和发送是为了让确认状态机能干净地界定「输入完成」这个时刻。
    """
    if message.kind == "text":
        return _compose_text(page, message.content or "", dry_run=dry_run)
    if message.kind == "sticker":
        return _compose_sticker(page, message.sticker or "", dry_run=dry_run)
    if message.kind == "image":
        return _compose_image(page, message.path, dry_run=dry_run)
    if message.kind == "random":
        # 正常流程里 pick_message 已经展开过了，到这里说明调用方式有问题
        raise ComposeError("random 消息应先经 pick_message 展开")
    raise ComposeError(f"未知的消息类型：{message.kind}")


def send(page: Any, *, via_button: bool = True, timeout_ms: int = 8_000) -> None:
    """触发发送。

    优先点发送按钮而不是按回车 —— 某些版本的页面对回车键的处理不一致
    （有时是换行），而按钮的语义是确定的。

    按钮找不到时退回回车。
    """
    button = sel.first_present(page, sel.SEND_BUTTON_CANDIDATES)
    if button is not None and via_button:
        try:
            if button.is_enabled():
                button.click(timeout=timeout_ms)
                LOGGER.debug("已点击发送按钮")
                return
            LOGGER.debug("发送按钮不可用，改用回车")
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("点击发送按钮失败（%s），改用回车", exc)

    input_box = sel.first_present(page, sel.COMPOSER_INPUT_CANDIDATES)
    if input_box is None:
        raise ComposeError("找不到输入框，无法发送")

    try:
        input_box.press("Enter")
        LOGGER.debug("已按回车发送")
    except Exception as exc:
        raise ComposeError(f"回车发送失败：{type(exc).__name__}: {exc}") from exc


def clear_input(page: Any) -> None:
    """清空输入框。输入失败后要调用，避免残留内容在下次发送时被带出去。"""
    input_box = sel.first_present(page, sel.COMPOSER_INPUT_CANDIDATES)
    if input_box is None:
        return
    try:
        input_box.click()
        input_box.press("Control+A")
        input_box.press("Delete")
    except Exception as exc:  # noqa: BLE001
        LOGGER.debug("清空输入框失败：%s", exc)


def read_input_text(page: Any) -> str:
    """读输入框里的当前内容。

    ⚠️ 关键：**必须剔除零宽字符**，否则「输入框是不是空的」永远判错。

    抖音的编辑器里永远带着一个含 U+200B 的 ``data-enter`` span，
    ``inner_text()`` 因此至少返回一个零宽空格。用它判断「消息发出去了没」
    会得到**假阴性 → 误报失败 → 触发重试 → 重复发送**。
    实测一次任务因此重复发了多条消息。
    """
    input_box = sel.first_present(page, sel.COMPOSER_INPUT_CANDIDATES)
    if input_box is None:
        return ""
    try:
        raw = input_box.inner_text() or ""
    except Exception:  # noqa: BLE001
        return ""
    return _clean_text(raw)


# =============================================================================
# 各类型消息的输入实现
# =============================================================================


def _compose_text(page: Any, text: str, *, dry_run: bool) -> ComposeResult:
    text = (text or "").strip()
    if not text:
        raise ComposeError("文本消息内容为空")

    input_box = sel.first_present(page, sel.COMPOSER_INPUT_CANDIDATES)
    if input_box is None:
        raise ComposeError(
            "找不到消息输入框。可能登录态已失效，或抖音前端结构有变化。"
            f"试过：{sel.describe_candidates(sel.COMPOSER_INPUT_CANDIDATES)}"
        )

    if dry_run:
        LOGGER.info("[演练] 本应输入文本：%s", text)
        return ComposeResult(kind="text", text=text, detail="演练模式，未真正输入")

    try:
        input_box.click()
    except Exception as exc:
        raise ComposeError(f"点击输入框失败：{type(exc).__name__}: {exc}") from exc

    # insert_text 不走键盘事件，速度快且不会被输入法干扰。
    # 多行文本在这里会被保留换行。
    try:
        page.keyboard.insert_text(text)
    except Exception as exc:  # noqa: BLE001
        # 某些环境（尤其是无头模式下的旧版本）不支持 insert_text，退回 type
        LOGGER.debug("insert_text 失败（%s），改用 type", exc)
        try:
            input_box.type(text, delay=0)
        except Exception as inner:
            raise ComposeError(f"输入文本失败：{type(inner).__name__}: {inner}") from inner

    return ComposeResult(kind="text", text=text)


def _compose_sticker(page: Any, sticker: str, *, dry_run: bool) -> ComposeResult:
    """选择一个表情。

    抖音的表情面板结构不固定，这里按四种方式依次尝试定位：
    描述文本 → 无障碍名 → 属性名 → 序号。
    实际使用中通常是「按描述找」，其余是兜底。
    """
    sticker = (sticker or "").strip()
    if not sticker:
        raise ComposeError("表情消息未指定表情")

    emoji_button = sel.first_present(page, sel.EMOJI_BUTTON_CANDIDATES)
    if emoji_button is None:
        raise ComposeError(
            f"找不到表情按钮。试过：{sel.describe_candidates(sel.EMOJI_BUTTON_CANDIDATES)}"
        )

    if dry_run:
        LOGGER.info("[演练] 本应选择表情：%s", sticker)
        return ComposeResult(kind="sticker", text=sticker, detail="演练模式，未真正选择")

    try:
        emoji_button.click()
    except Exception as exc:
        raise ComposeError(f"点击表情按钮失败：{type(exc).__name__}: {exc}") from exc

    panel = sel.first_present(page, sel.EMOJI_PANEL_CANDIDATES, timeout_ms=5_000)
    if panel is None:
        raise ComposeError("表情面板没有弹出")

    item = _locate_sticker_item(panel, sticker)
    if item is None:
        raise StickerNotFoundError(
            f"表情面板里找不到「{sticker}」。\n"
            "建议改用文本消息，或者在抖音里确认该表情的准确名称。"
        )

    try:
        item.click()
    except Exception as exc:
        raise ComposeError(f"点击表情失败：{type(exc).__name__}: {exc}") from exc

    return ComposeResult(kind="sticker", text=sticker)


def _locate_sticker_item(panel: Any, sticker: str):
    """在表情面板里按优先级定位一个表情。"""
    # 1. 描述文本精确匹配
    for candidate in (
        f'[title="{sticker}"]',
        f'[aria-label="{sticker}"]',
        f'img[alt="{sticker}"]',
        f'text="{sticker}"',
    ):
        try:
            locator = panel.locator(candidate).first
            if locator.count() > 0:
                return locator
        except Exception:  # noqa: BLE001
            continue

    # 2. 无障碍名称包含匹配
    for candidate in sel.EMOJI_ITEM_CANDIDATES:
        try:
            items = panel.locator(candidate)
            total = min(items.count(), 200)
            for index in range(total):
                item = items.nth(index)
                for attr in ("aria-label", "title"):
                    label = item.get_attribute(attr)
                    if label and sticker in label:
                        return item
        except Exception:  # noqa: BLE001
            continue

    # 3. 序号兜底（sticker 写成纯数字时）
    if sticker.isdigit():
        try:
            items = panel.locator(sel.EMOJI_ITEM_CANDIDATES[0])
            index = int(sticker)
            if 0 <= index < items.count():
                return items.nth(index)
        except Exception:  # noqa: BLE001
            pass

    return None


def _compose_image(page: Any, path: Path | None, *, dry_run: bool) -> ComposeResult:
    """上传一张图片。

    用 ``set_input_files`` 直接往 file input 里塞，不用走系统文件选择框
    （无头环境里根本没有对话框可点）。
    """
    if path is None:
        raise ComposeError("图片消息未指定路径")

    path = Path(path)
    if not path.is_file():
        raise ComposeError(f"图片文件不存在：{path}")

    if dry_run:
        LOGGER.info("[演练] 本应上传图片：%s", path)
        return ComposeResult(kind="image", text=str(path), detail="演练模式，未真正上传")

    # 优先直接用页面上的 file input
    file_input = sel.first_present(page, sel.IMAGE_UPLOAD_CANDIDATES)
    if file_input is None:
        # 没有现成的 input，得先点图片按钮把它唤出来
        button = sel.first_present(page, sel.IMAGE_BUTTON_CANDIDATES)
        if button is not None:
            try:
                with page.expect_file_chooser(timeout=5_000) as chooser_info:
                    button.click()
                chooser = chooser_info.value
                chooser.set_files(str(path))
                return ComposeResult(kind="image", text=str(path))
            except Exception as exc:
                raise ComposeError(
                    f"打开图片选择器失败：{type(exc).__name__}: {exc}"
                ) from exc

        raise ComposeError(
            "找不到图片上传入口。"
            f"试过：{sel.describe_candidates(sel.IMAGE_UPLOAD_CANDIDATES)}"
        )

    try:
        file_input.set_input_files(str(path))
    except Exception as exc:
        raise ComposeError(f"设置图片文件失败：{type(exc).__name__}: {exc}") from exc

    return ComposeResult(kind="image", text=str(path))
