# 打包成 Windows 安装包

> **2.0.0 起正式发布只使用仓库根目录的 `build_release.bat`。** 该命令生成
> `fronted/release/JingYuan-Setup-<version>.exe` 与 `JingYuan-Portable-<version>.exe`，并完整重建
> Electron 外壳和 Python sidecar。本文其余 Inno Setup 流程仅作为历史备用链路保留，不得用于正式发布。

把"前端 + 后端 + SQLite"打成**一个 Windows 安装包**（`FinResearch-Setup.exe`），
安装后双击桌面图标即弹出**独立应用窗口**（pywebview + 系统 WebView2，真软件，非浏览器）。
**只打包运行所需**（无源码、无 node_modules、无 .git、无测试）。

## 前置（构建机，一次性）

| 工具 | 版本 | 说明 |
|---|---|---|
| **Python** | **3.12+（3.14 已实测可用）** | PyInstaller 6.21+ 在 3.14 实测通过；3.12 仍是安全下限 |
| Node.js + pnpm | 任意现代版 | 构建前端 |
| Inno Setup 6 | 6.x | 生成安装包；装到默认路径 `C:\Program Files (x86)\Inno Setup 6` |

> 运行机（父母电脑）无需 Python/Node——安装包已含 Python 运行时与全部依赖。
> 仅需 **Win10/11**（自带 WebView2；极旧系统装 [Evergreen Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)）。

## 一键打包

在 `backend/` 目录双击或命令行运行：

```bat
packaging\build_installer.bat
```

它会依次：
1. `pnpm install && pnpm build`（前端）→ 复制 `fronted/dist` 到 `backend/src/finresearch/static`；
2. `pip install -e ".[desktop,packaging]"`（后端依赖 + pywebview + pyinstaller）；
3. `pyinstaller packaging/finresearch.spec` → `dist/FinResearch/`（onedir，仅运行时）；
4. `ISCC packaging/installer.iss` → **`packaging/Output/FinResearch-Setup.exe`**。

把 `FinResearch-Setup.exe` 拷给父母，双击安装 → 开始菜单/桌面图标 → 开盖即用。

## 数据落点

- **程序**：`C:\Program Files\FinResearch\`（只读，安装包内容）。
- **用户数据 + SQLite**：`%APPDATA%\FinResearch\finresearch.db`（可写，跨升级保留）。
- 卸载只删程序目录，用户数据保留（用户可手动删 `%APPDATA%\FinResearch`）。

## 体积说明（已剔除无用内容）

安装包不可避免会有几百 MB：Python 运行时 + pandas + langgraph + akshare + pywebview
都是**运行必需**。已通过 `excludes`（tkinter/pytest/IPython/jupyter 等）和"仅打包 onedir 运行时、
不含源码/node_modules/.git"做到最小。要进一步瘦身只能砍功能（如去掉 akshare）。

## 排错

- **PyInstaller 漏导入**（运行报 `ModuleNotFoundError`）：在 `finresearch.spec` 的
  `hiddenimports` 或 `collect_all(...)` 列表里加上缺的包，重新跑 step 3。
- **窗口白屏/打不开**：多半是 WebView2 缺失，装 Evergreen Runtime；或临时把 spec 里
  `console=False` 改 `True` 重打包看日志。
- **akshare 取不到数据**：父母电脑到东方财富的网络问题（非打包问题），后端会优雅降级。
- **想看后端日志**：把 `finresearch.spec` 的 `console=True` 重打包，会带一个控制台窗口。

## 分别构建（不跑 bat）

```bat
:: 前端
cd ..\fronted && pnpm install && pnpm build
xcopy /E /I /Y dist ..\backend\src\finresearch\static

:: 后端 exe
cd ..\backend
pip install -e ".[desktop,packaging]"
pyinstaller packaging\finresearch.spec --noconfirm --clean

:: 安装包
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\installer.iss
```
