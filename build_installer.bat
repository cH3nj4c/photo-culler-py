@echo off
setlocal
cd /d "%~dp0"

set PY=.venv-build\Scripts\python.exe
if not exist "%PY%" (
  echo Missing .venv-build — run build_exe.bat first.
  exit /b 1
)

if not exist "dist\PhotoCuller\Photo Culler.exe" (
  echo Missing dist\PhotoCuller — run build_exe.bat first.
  exit /b 1
)

if not exist "dist\PhotoCuller\bin" mkdir "dist\PhotoCuller\bin"
copy /Y "dist\PhotoCuller\_internal\tcl86t.dll" "dist\PhotoCuller\bin\" >nul
copy /Y "dist\PhotoCuller\_internal\tk86t.dll" "dist\PhotoCuller\bin\" >nul

echo Building one-file installer...
set CODEBUDDY_SAFE_DELETE_ENABLED=0
"%PY%" -m PyInstaller --noconfirm --clean Installer.spec
if errorlevel 1 exit /b 1

REM Report the real output name; it carries config.APP_VERSION (see version_info.py).
REM Printed by Python rather than resolved with `for /f`: wrapping a quoted
REM command path in for/f makes cmd strip the quote and the name comes back
REM empty (verified — both the '...' and usebackq `...` forms fail).
echo.
"%PY%" -c "import version_info; print('Installer: dist/' + version_info.INSTALLER_BASENAME + '.exe')"
endlocal
