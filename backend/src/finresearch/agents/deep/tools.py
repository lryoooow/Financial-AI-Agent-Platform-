"""兼容 v1.x 的工具注册表导入路径。"""

import sys

from ...tools import registry as _registry

sys.modules[__name__] = _registry
