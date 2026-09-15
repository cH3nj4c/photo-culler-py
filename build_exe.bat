@echo off
setlocal
cd /d "%~dp0"

set PY=.venv-build\Scripts\python.exe
if not exist "%PY%" (
  echo Creating build venv...
  python -m venv .venv-build || exit /b 1
  .venv-build\Scripts\python.exe -m pip install -U pip wheel || exit /b 1
  .venv-build\Scripts\python.exe -m pip install Pillow numpy rawpy pyinstaller || exit /b 1
)

echo Building Photo Culler (onedir)...
"%PY%" -m PyInstaller --noconfirm --clean PhotoCuller.spec
if errorlevel 1 exit /b 1

REM Tk resolves ..\bin\tk86t.dll relative to _tk_data; copy DLLs next to the app root.
if not exist "dist\PhotoCuller\bin" mkdir "dist\PhotoCuller\bin"
copy /Y "dist\PhotoCuller\_internal\tcl86t.dll" "dist\PhotoCuller\bin\" >nul
copy /Y "dist\PhotoCuller\_internal\tk86t.dll" "dist\PhotoCuller\bin\" >nul

echo.
echo Output: dist\PhotoCuller\Photo Culler.exe
endlocal
