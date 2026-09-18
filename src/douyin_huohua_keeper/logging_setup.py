"""日志落盘。

为什么单独一个模块：项目里所有日志都走标准库 ``logging``，但**默认只打控制台**。
工作台的「日志」页读的是 ``log_dir`` 下的文件 —— 不落盘，那个页面就永远是空的。

回归背景：曾经完全没有日志配置。表现为「点『刷新日志』显示为空」，
而且接口出 500 时日志里查不到任何痕迹，排障变成了盲猜。

约定：
- 所有日志写 ``<log_dir>/huohua.log``，按大小滚动（5MB × 5 份）。
- **幂等**：重复调用不会叠加 handler，也不会重复写。
- 只加文件 handler —— 控制台那块交给 uvicorn / 终端，避免重复刷屏。
"""

from __future__ import annotations

import contextlib
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

LOG_FILENAME = "huohua.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 5

# 用来识别「本模块加的 handler」，重复调用时先摘掉旧的
_HANDLER_FLAG = "_huohua_file_handler"

_configured_path: Path | None = None


def configure_logging(settings: Any, *, force: bool = False) -> Path | None:
    """把根 logger 接到 ``log_dir`` 下的滚动文件，返回日志文件路径。

    落盘失败（目录不可写等）**不会抛异常** —— 日志配不上不该让服务起不来，
    但会留下一条 warning。返回 ``None`` 表示没配上。
    """
    global _configured_path
    if _configured_path is not None and not force:
        return _configured_path

    log_dir = Path(getattr(settings, "log_dir", "logs"))
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        LOGGER.warning("无法创建日志目录 %s：%s（日志将只打控制台）", log_dir, exc)
        return None

    path = log_dir / LOG_FILENAME

    root = logging.getLogger()
    level = getattr(logging, str(getattr(settings, "log_level", "INFO")).upper(), logging.INFO)
    root.setLevel(level)

    # force 重配时先摘掉上一次加的，避免同一条日志写两遍
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_FLAG, False):
            root.removeHandler(handler)
            with contextlib.suppress(Exception):
                handler.close()

    try:
        handler = RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
    except OSError as exc:
        LOGGER.warning("无法打开日志文件 %s：%s（日志将只打控制台）", path, exc)
        return None

    setattr(handler, _HANDLER_FLAG, True)
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(handler)

    _configured_path = path
    return path


def current_log_path() -> Path | None:
    """当前生效的日志文件路径（没配则为 None）。"""
    return _configured_path
