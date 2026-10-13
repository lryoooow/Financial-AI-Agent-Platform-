@echo off
REM JingYuan one-click packaging (Electron + electron-builder).
REM Output: fronted\release\JingYuan-Setup-*.exe + JingYuan-Portable-*.exe.
REM
REM KEEP THIS FILE PURE ASCII. cmd.exe parses .bat under the OEM codepage (GBK on zh-CN).
REM UTF-8 CJK bytes there eat the line terminator and glue the next line onto the comment,
REM which silently skips the following command. That is how `cd /d %~dp0` used to vanish:
REM the script then only worked when the caller's cwd already was the script directory,
REM and otherwise pushd/popd desynced until `pip install -e .` ran inside fronted\.
setlocal
cd /d "%~dp0" || goto :err

REM Clean old release products (version-specific filenames would otherwise accumulate)
if exist fronted\release\*.exe del /q fronted\release\*.exe
if exist fronted\release\*.blockmap del /q fronted\release\*.blockmap

REM npmmirror speeds up electron / electron-builder binary downloads in CN
set ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/
set ELECTRON_BUILDER_BINARIES_MIRROR=https://npmmirror.com/mirrors/electron-builder-binaries/

REM electron-builder invokes pnpm directly while Corepack may be read-only next to node.exe.
REM Install shims into a writable temp directory and expose them only for this build.
set "COREPACK_SHIMS=%TEMP%\jingyuan-corepack"
if not exist "%COREPACK_SHIMS%" mkdir "%COREPACK_SHIMS%"
call corepack enable --install-directory "%COREPACK_SHIMS%"
if errorlevel 1 goto :err
set "PATH=%COREPACK_SHIMS%;%PATH%"

echo === [1/4] frontend: pnpm install + build ===
pushd fronted || goto :err
call corepack pnpm install --frozen-lockfile
if errorlevel 1 goto :err
call corepack pnpm build
if errorlevel 1 goto :err
if exist ..\backend\src\finresearch\static rmdir /s /q ..\backend\src\finresearch\static
xcopy /E /I /Y dist ..\backend\src\finresearch\static
if errorlevel 1 goto :err
popd

echo === [2/4] backend: uv sync ===
pushd backend || goto :err
python -m uv sync --extra packaging
if errorlevel 1 goto :err

echo === [3/4] backend: PyInstaller (server entry) ===
python -m uv run pyinstaller packaging\finresearch.spec --noconfirm --clean
if errorlevel 1 goto :err
popd

echo === [4/4] electron-builder --win --x64 ===
pushd fronted || goto :err
call corepack pnpm exec electron-builder --win --x64
if errorlevel 1 goto :err
popd

echo.
echo ============================================================
echo  DONE
echo   installer : fronted\release\JingYuan-Setup-*.exe
echo   portable  : fronted\release\JingYuan-Portable-*.exe
echo ============================================================
goto :eof

:err
echo.
echo [X] BUILD FAILED. See errors above.
exit /b 1
