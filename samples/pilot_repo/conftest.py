"""让 ``python -m pytest`` 开箱可用。

把 ``src`` 放进 ``sys.path`` —— 否则 ``import tinylib`` 会失败，而一个
"跑测试要先设置 PYTHONPATH" 的项目会让每一次自动化都多一个前置步骤。
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
