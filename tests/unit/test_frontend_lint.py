"""前端 JS 的静态检查。

前端是零构建的原生 JS，没有类型检查兜底 —— 这类低级错误（用了没声明的变量、
漏传的参数）在浏览器里只有点到那个按钮才会暴露。实测踩过：
``contactRow`` 加参数时漏改签名，函数体里的 ``reload`` 直接 ReferenceError。

这里直接调 ESLint 跑一遍。**没装工具链就跳过**（``npm install`` 后可用），
所以不装 Node 的环境也不会因此失败。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = ROOT / "src" / "douyin_huohua_keeper" / "workbench" / "static"
CONFIG = ROOT / "eslint.config.mjs"


def _eslint_bin() -> Path:
    """node_modules 里的 eslint 可执行文件（Windows 是 .cmd）。"""
    name = "eslint.cmd" if os.name == "nt" else "eslint"
    return ROOT / "node_modules" / ".bin" / name


def test_frontend_js_has_no_undefined_identifiers() -> None:
    eslint = _eslint_bin()
    if not eslint.is_file():
        pytest.skip("未安装前端 lint 工具链（在项目根执行 npm install 后即可启用）")

    result = subprocess.run(
        [str(eslint), "--config", str(CONFIG), str(STATIC_DIR)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert result.returncode == 0, f"ESLint 未通过：\n{result.stdout}\n{result.stderr}"
