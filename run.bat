@echo off
REM Launch Photo Culler from source with an interpreter that can actually run it.
REM
REM Why this exists: double-clicking app.py, or running "python app.py" with a
REM bare system Python, fails with "No module named 'numpy'" and looks like a
REM broken app. It is not — that interpreter simply has none of the runtime
REM dependencies. This picks the project's own venv instead.
setlocal
cd /d "%~dp0"

set "PY="
set "TK_ONLY="

for %%V in (.venv .venv-build) do (
  if exist "%%V\Scripts\python.exe" (
    REM First choice: a venv that can run the GPU shell.
    if not defined PY (
      "%%V\Scripts\python.exe" -c "import numpy, PIL, PySide6, vispy, OpenGL" >nul 2>&1
      if not errorlevel 1 set "PY=%%V\Scripts\python.exe"
    )
    REM Second choice: a venv that can at least run the Tk shell.
    if not defined TK_ONLY (
      "%%V\Scripts\python.exe" -c "import numpy, PIL" >nul 2>&1
      if not errorlevel 1 set "TK_ONLY=%%V\Scripts\python.exe"
    )
  )
)

if defined PY (
  echo Starting Photo Culler with the GPU interface ^(%PY%^)...
  "%PY%" app.py %*
  goto :done
)

if defined TK_ONLY (
  echo Starting Photo Culler with the Tkinter interface ^(%TK_ONLY%^)...
  echo GPU dependencies are not installed in that environment.
  echo To get the GPU interface: %TK_ONLY% -m pip install PySide6 vispy PyOpenGL
  "%TK_ONLY%" app.py %*
  goto :done
)

echo No usable Python environment was found next to this script.
echo Run build_exe.bat once to create .venv-build and install the dependencies.
echo.
exit /b 1

:done
endlocal
