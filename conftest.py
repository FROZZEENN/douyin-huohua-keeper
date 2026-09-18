"""pytest 全局配置。

存在的主要理由：**让 ``pytest`` 在源码目录下能直接跑通**，不需要先
``pip install -e .``，也不需要手动设 ``PYTHONPATH``。

把 ``src/`` 加进 ``sys.path`` 之后，``import douyin_huohua_keeper`` 就能
解析到本地源码。这对贡献者很重要 —— 克隆下来第一件事就是跑测试，
如果那一步就要先搞环境变量，很多人会直接放弃。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def pytest_collection_modifyitems(config, items):
    """按所在目录自动补 pytest 标记。

    为什么需要它：``pyproject.toml`` 里注册了 ``unit`` / ``integration`` /
    ``live`` 三个标记，但**没有任何测试文件主动用过它们**。于是 CI 里的
    ``pytest tests/unit -m unit`` 会选中 0 个测试 —— pytest 以
    **退出码 5（no tests ran）**结束，step 失败、整个 test job 变红。
    （实测：那两个测试 step 的退出码都是 5。）

    这里按目录自动补标记，注册过的标记才算真的生效。
    """
    for item in items:
        parts = Path(str(item.fspath)).parts
        if "unit" in parts:
            item.add_marker(pytest.mark.unit)
        elif "integration" in parts:
            item.add_marker(pytest.mark.integration)
