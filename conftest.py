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

_SRC = Path(__file__).parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
