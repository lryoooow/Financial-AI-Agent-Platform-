# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包规格（onedir）。

已实测：PyInstaller 6.21+ 在 Python 3.14 上可用（无需降级 3.12）。
关键修复：所有路径必须用 SPECPATH 推导为绝对路径——
之前写成相对的 "run_desktop.py"，PyInstaller 按 spec 所在目录(packaging/)解析，
会去 packaging/run_desktop.py 找而直接报 not found，构建第 0 步就死。

由 build_installer.bat 调用，或：pyinstaller packaging/finresearch.spec --noconfirm --clean
产物：dist/FinResearch/FinResearch.exe（onedir，连同 _internal 依赖被 Inno Setup 打包）。
"""

import glob
import os
from PyInstaller.utils.hooks import collect_all, collect_submodules, collect_data_files

# SPECPATH 是 PyInstaller 注入的"spec 所在目录"（即 packaging/），不是 spec 文件路径。
# 据此推出项目根，绝不依赖 cwd。
_SPEC_DIR = os.path.abspath(SPECPATH)                     # .../backend/packaging
_PROJ_DIR = os.path.dirname(_SPEC_DIR)                    # .../backend
_SRC_DIR = os.path.join(_PROJ_DIR, "src")

datas = []
binaries = []
hiddenimports = []

# 重型 / 动态导入的库：全量收集（数据 + 二进制 + 子模块）
for pkg in ["akshare", "langgraph", "pandas_ta_classic", "chinese_calendar", "sqlite_vec"]:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h
# sqlite_vec 的 vec0 可加载扩展（.dll）必须随包：collect_all 已带 binaries，
# 再显式兜一份防止上游打包布局变化（loadable_path 指向的文件缺失时记忆功能优雅降级）
try:
    import sqlite_vec as _sv

    binaries += [(os.path.join(os.path.dirname(_sv.__file__), "vec0.dll"), "sqlite_vec")]
except Exception:
    pass

# langchain 系列 / sqlite 检查点：按子模块收集
for pkg in [
    "langchain",
    "langchain_core",
    "langchain_openai",
    "langgraph.checkpoint",
    "aiosqlite",
    "docx",
    "reportlab",
    "PIL",
    # deepagents 对话链路（agents/deep/*）。中间件/后端有动态导入（graph.py 用 import_module
    # 解析 profile 与 model），静态分析追不全，按子模块收。
    "deepagents",
    # langchain_anthropic 必须打进去：deepagents/graph.py 顶层就 `from langchain_anthropic
    # import ChatAnthropic`（默认模型构造器 + AnthropicPromptCachingMiddleware），
    # 排它会直接 ImportError——即便本项目一行 Anthropic 都不用。已实测 import 确实发生。
    "langchain_anthropic",
    "wcmatch",
]:
    hiddenimports += collect_submodules(pkg)

# 本包全部子模块：desktop.py 用字符串 "finresearch.main:app" 启动 uvicorn，
# 静态分析追不到 main/agents/api/db/tools，必须显式收集，否则运行时 import 失败。
hiddenimports += collect_submodules("finresearch")

# 排除 postgres 检查点（桌面用 SQLite，postgres 分支永不触发）：从 collect_all 结果中剔除
hiddenimports = [h for h in hiddenimports if not h.startswith("langgraph.checkpoint.postgres")]

# 本包数据（报告模板）。即便没 pip install -e . 也兜底显式收集
datas += collect_data_files("finresearch")
datas += collect_data_files("reportlab")
_tmpl_dir = os.path.join(_SRC_DIR, "finresearch", "report_templates")
datas += [(f, "finresearch/report_templates") for f in glob.glob(os.path.join(_tmpl_dir, "*.html"))]
_data_dir = os.path.join(_SRC_DIR, "finresearch", "data")
datas += [(f, "finresearch/data") for f in glob.glob(os.path.join(_data_dir, "*.json"))]

# 前端构建产物（build_installer.bat 已复制到 src/finresearch/static）
static = os.path.join(_SRC_DIR, "finresearch", "static")
if os.path.isdir(static):
    datas += [(static, "static")]

# uvicorn / 桌面运行时子模块（静态分析发现不了）
hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "aiosqlite",
]

a = Analysis(
    [os.path.join(_PROJ_DIR, "run_server.py")],   # 服务端入口（无窗口）；Electron 作为外壳拉起它
    pathex=[_SRC_DIR],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter", "pytest", "IPython", "notebook", "jupyter", "jupyter_client",
        # 本应用用不到、却被传递依赖的可选导入拖入图的重型库（砍掉约 1.3GB）
        "torch", "torchvision", "torchaudio",
        "cupy", "cupy_backends", "cupyx",
        "llvmlite", "numba",
        "matplotlib", "mpl_toolkits",
        "nltk",
        # ── 1.3.0 体积精简（已逐项验证零运行时依赖）──
        # scipy（70MB）：akshare + pandas_ta_classic 均零引用，指标计算无 scipy 正常
        "scipy", "scipy.libs",
        # psycopg / postgres（6.5MB）：桌面默认 SQLite，检查点 postgres 分支永不触发
        "psycopg", "psycopg_binary",
        "langgraph.checkpoint.postgres", "langgraph.checkpoint.postgres.aio",
        "asyncpg",
        # pythonnet（3.3MB）：.NET 桥接，模块名 clr；akshare + 本项目源码均零引用
        "clr", "pythonnet", "pythonnet_dotnet", "System", "System.Runtime",
        # 死依赖（pyproject 声明了却源码零 import）
        "empyrical", "empyrical_reloaded", "numpy_financial", "npf",
        # 其他明确不用的重型可选依赖
        "pyarrow", "sklearn", "scikit-learn", "seaborn", "plotly", "bokeh",
        "sympy", "mpmath",
        # ── deepagents 传递依赖里用不到的部分 ──
        # langchain_google_genai / google-genai：deepagents 的硬声明依赖，但全仓无任何
        # import 点（只在 _models.resolve_model 里按模型字符串走 import_module 惰性解析）。
        # 本项目 build_llm 传的是 ChatOpenAI 实例而非字符串，provider 解析分支根本不触发。
        # 已实测：构建完 agent 后 'langchain_google_genai' not in sys.modules。
        # 连带 google-genai 自己拖的 google-auth / websockets 一并省掉。
        "langchain_google_genai", "google.genai", "google_genai",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FinResearch",
    console=False,   # 真软件：无控制台黑框（调试时临时改 True 看日志）
    icon=os.path.join(_SPEC_DIR, "app.ico"),  # 统一品牌图标（make_brand_assets.py 生成）
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="FinResearch",
)
