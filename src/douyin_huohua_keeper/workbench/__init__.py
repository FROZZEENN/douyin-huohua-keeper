"""网页工作台。

一个 FastAPI 应用 + 一份零构建的原生前端，目标是让你在手机上也能完成
「扫码登录 / 改配置 / 看历史」这三件事，而不必 SSH 进服务器敲命令。

- ``app``      —— FastAPI 实例装配、中间件（令牌校验、IP 白名单）、静态资源挂载
- ``routes``   —— 按资源拆分的 REST 接口：账号、联系人、任务、运行历史、系统
- ``static``   —— 原生 HTML/CSS/JS，没有 npm，没有打包，改完刷新就生效

这一层只做「参数校验 + 调用下层 + 序列化响应」，业务逻辑一律下沉。
"""

from __future__ import annotations

from .app import AppState, create_app, serve

__all__ = ["AppState", "create_app", "serve"]
