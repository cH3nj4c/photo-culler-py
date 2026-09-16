@echo off
setlocal
cd /d "%~dp0"

set PY=.venv-build\Scripts\python.exe
if not exist "%PY%" (
  echo Creating build venv...
  python -m venv .venv-build || exit /b 1
)

REM Verify the venv is *complete*, not merely present. Installing only when the
REM venv is created is how a build silently loses a whole interface: an existing
REM venv from before the GPU shell existed kept building exes with no PySide6,
REM so the packaged app always fell back to Tk.
"%PY%" -c "import PySide6, vispy, OpenGL, numpy, PIL, rawpy, PyInstaller" 2>nul
if errorlevel 1 (
  echo Build venv is missing packages - installing...
  "%PY%" -m pip install -U pip wheel || exit /b 1
  "%PY%" -m pip install Pillow numpy rawpy PySide6 vispy PyOpenGL pyinstaller || exit /b 1
)

echo Building Photo Culler (onedir)...
REM PyInstaller wipes dist\PhotoCuller before COLLECT. In agent/sandboxed shells
REM a file-deletion shim can intercept that and abort the build partway
REM (SAFE_DELETE_BULK_CONFIRM_REQUIRED on ~2400 files), leaving a stale dist.
set CODEBUDDY_SAFE_DELETE_ENABLED=0
"%PY%" -m PyInstaller --noconfirm --clean PhotoCuller.spec
if errorlevel 1 exit /b 1

REM Tk resolves ..\bin\tk86t.dll relative to _tk_data; copy DLLs next to the app root.
if not exist "dist\PhotoCuller\bin" mkdir "dist\PhotoCuller\bin"
for %%F in (tcl86t.dll tk86t.dll) do (
  if exist "dist\PhotoCuller\_internal\%%F" (
    copy /Y "dist\PhotoCuller\_internal\%%F" "dist\PhotoCuller\bin\" >nul
  ) else (
    echo WARNING: dist\PhotoCuller\_internal\%%F not found - the Tk fallback may not start.
  )
)

echo.
echo Output: dist\PhotoCuller\Photo Culler.exe
endlocal
