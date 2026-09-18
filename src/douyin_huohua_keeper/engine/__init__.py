"""发送引擎：把「今天要给谁发什么」真正落到抖音网页端。

对外主要暴露 :class:`Engine` 与 :func:`send_to_contact`，内部拆成五块：

- ``browser``    —— 浏览器生命周期（约束在专用线程里）、登录态加载
- ``auth``       —— 三层登录态检查（本地文件时效 / 页面探测 / 输入框就绪）
- ``selectors``  —— 所有页面选择器集中管理，改版只需改这一个文件
- ``navigator``  —— 会话列表读取、联系人定位、打开会话（带防发错人校验）
- ``composer``   —— 依据 :class:`~douyin_huohua_keeper.models.Message` 构建并输入内容
- ``confirmer``  —— 发送确认状态机，观察终态而不是盲信回车
- ``sender``     —— 编排上述环节的门面

设计约束：这一层不做重试决策、不发通知、不写磁盘。
它只回答一个问题 —— 「这一次发送，成功了还是失败了，失败属于哪一类」。
重试、告警、落盘分别由 ``scheduler`` / ``notify`` / ``store`` 负责。
"""

from __future__ import annotations

from .browser import BrowserContext, BrowserError, BrowserNotStartedError, BrowserSession
from .confirmer import ConfirmOutcome, ConfirmState, confirm_sent
from .sender import (
    AuthExpiredError,
    Engine,
    EngineError,
    RiskSuspectedError,
    SendOutcome,
    describe_outcome,
)

__all__ = [
    "AuthExpiredError",
    "BrowserContext",
    "BrowserError",
    "BrowserNotStartedError",
    "BrowserSession",
    "ConfirmOutcome",
    "ConfirmState",
    "Engine",
    "EngineError",
    "RiskSuspectedError",
    "SendOutcome",
    "confirm_sent",
    "describe_outcome",
]
