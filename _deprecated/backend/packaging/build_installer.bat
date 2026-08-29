@echo off
REM Build JingYuan Windows app + installer.
REM Pure ASCII on purpose: Chinese Windows cmd parses .bat as GBK, so CJK here would garble.
setlocal
cd /d %~dp0\..

if not exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" (
  echo [!] Inno Setup 6 NOT found - only the final Setup.exe step is skipped; steps 1-3 still run.
  echo     Install from https://jrsoftware.org/isdl.php , then run:
  echo     "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\installer.iss
  echo.
)

echo === [1/4] Build frontend (pnpm) ===
pushd ..\fronted
call pnpm install
if errorlevel 1 goto :err
call pnpm build
if errorlevel 1 goto :err
if exist ..\backend\src\finresearch\static rmdir /s /q ..\backend\src\finresearch\static
xcopy /E /I /Y dist ..\backend\src\finresearch\static
if errorlevel 1 goto :err
popd

echo === [2/4] Install backend deps (desktop + packaging) ===
pip install -e ".[desktop,packaging]"
if errorlevel 1 goto :err

echo === [3/4] PyInstaller (onedir) ===
python -m PyInstaller packaging\finresearch.spec --noconfirm --clean
if errorlevel 1 goto :err

echo === [4/4] Inno Setup ===
set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo [!] Inno Setup 6 not found. Skipping installer step.
  echo     App bundle is ready: dist\FinResearch\FinResearch.exe
  goto :done
)
"%ISCC%" packaging\installer.iss
if errorlevel 1 goto :err

:done
echo.
echo ============================================================
echo  DONE.
echo  App bundle : dist\FinResearch\FinResearch.exe
echo  Installer  : packaging\Output\FinResearch-Setup.exe  (needs Inno Setup)
echo ============================================================
goto :eof

:err
echo.
echo [X] BUILD FAILED. See errors above.
exit /b 1
