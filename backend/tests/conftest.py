"""测试全局隔离（v1.8.0）：知识库/画像库指向临时文件，绝不写入用户真实的 %APPDATA%/FinResearch。

在导入期设置环境变量，子进程用例（_*_impl.py）继承同一份环境。
"""

import os
import tempfile

os.environ.setdefault(
    "FINRESEARCH_KNOWLEDGE_DB", os.path.join(tempfile.mkdtemp(prefix="jy-kb-"), "knowledge.db")
)
os.environ.setdefault("FIN_TRUSTED_HOSTS", "testserver,localhost,127.0.0.1")
