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
"%PY%" -m PyInstaller --noconfirm --clean Installer.spec
if errorlevel 1 exit /b 1

echo.
echo Installer: dist\Photo-Culler-Setup.exe
endlocal
