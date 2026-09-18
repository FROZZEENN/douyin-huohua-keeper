"""健康检查。

唯一一个**不需要令牌**的接口 —— Docker healthcheck 和自检脚本要用它。

因此这里返回的信息必须克制：只说「活着」和版本号，不泄露配置、
不暴露账号状态、不给出任何可用于探测的信息。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ... import __version__

router = APIRouter(tags=["system"])


@router.get("/api/health")
async def health(request: Request) -> dict[str, object]:
    """存活探测。

    刻意保持极简。想了解运行详情请用 ``/api/system/overview``（需要令牌）。
    """
    return {
        "status": "ok",
        "version": __version__,
    }


@router.get("/api/version")
async def version() -> dict[str, str]:
    return {"version": __version__}
