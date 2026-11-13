"""兼容 v1.x 的导入路径。"""

import sys

from ..engine import checkpoints as _adapter

sys.modules[__name__] = _adapter
