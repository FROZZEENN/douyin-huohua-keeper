"""REST 路由的总装配。

按资源拆分，每个模块注册自己的端点。统一在这里挂上，便于一眼看全 API 面。

约定：
- 路径一律 ``/api/`` 开头
- 参数用 Pydantic 模型校验
- 响应字段名与落盘 JSON 一致（复用 ``models`` 里的序列化辅助）
- 路由函数保持薄：校验 → 调下层 → 序列化
"""

from __future__ import annotations

from fastapi import FastAPI

from . import accounts, contacts, health, runs, system, tasks


def register_routes(app: FastAPI) -> None:
    app.include_router(health.router)
    app.include_router(accounts.router)
    app.include_router(contacts.router)
    app.include_router(tasks.router)
    app.include_router(runs.router)
    app.include_router(system.router)


__all__ = ["register_routes"]
