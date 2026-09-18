"""douyin-huohua-keeper —— 让抖音火花不再因无人值守而中断。

顶层包只做两件事：
1. 暴露版本号
2. 描述各子包的职责边界

真实的逻辑都在子包里，这里保持极薄，避免出现循环导入。
"""

from __future__ import annotations

__all__ = ["PACKAGE_NAME", "__version__"]

__version__ = "0.1.0"

PACKAGE_NAME = "douyin-huohua-keeper"
