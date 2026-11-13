@echo off
rem Build CodexStatusMonitor.exe inside an isolated project-local venv.
rem The shared system Python is never modified. ASCII only.
setlocal
cd /d "%~dp0"

set "VENV_DIR=%~dp0.venv"
set "VENV_PY=%~dp0.venv\Scripts\python.exe"
set "OUT_EXE=build\dist\CodexStatusMonitor\CodexStatusMonitor.exe"

if exist "%VENV_PY%" goto check_tk

echo [1/4] Creating isolated venv ...
where py >nul 2>nul
if errorlevel 1 goto venv_by_python
py -3.11 -m venv "%VENV_DIR%"
if exist "%VENV_PY%" goto check_tk

:venv_by_python
python -m venv "%VENV_DIR%"
if not exist "%VENV_PY%" goto venv_failed

:check_tk
rem tkinter is mandatory; a Python built without Tcl/Tk cannot ship this GUI.
"%VENV_PY%" -c "import tkinter" >nul 2>nul
if errorlevel 1 goto no_tkinter

echo [2/4] Installing build dependencies into .venv ...
"%VENV_PY%" -m pip install --no-input --disable-pip-version-check -r requirements-dev.txt
if errorlevel 1 goto deps_failed

echo [3/4] Building executable ...
set "PYTHONPATH="
"%VENV_PY%" -m PyInstaller --noconfirm --clean --windowed --name CodexStatusMonitor ^
  --distpath "build\dist" ^
  --workpath "build\work" ^
  --specpath "build" ^
  --hidden-import paho.mqtt.client ^
  --hidden-import pystray._win32 ^
  --hidden-import pystray._util.win32 ^
  --hidden-import PIL.ImageDraw ^
  --add-data "%~dp0monitor_config.example.json;." ^
  monitor_app.py
if errorlevel 1 goto build_failed

echo [4/4] Copying config template next to the executable ...
copy /y "%~dp0monitor_config.example.json" "build\dist\CodexStatusMonitor\monitor_config.example.json" >nul
if errorlevel 1 goto build_failed

echo.
echo Build complete: %OUT_EXE%
pause
exit /b 0

:venv_failed
echo ERROR: failed to create .venv. Install Python 3.11+ first.
pause
exit /b 1

:no_tkinter
echo ERROR: the Python used to create .venv has no tkinter (Tcl/Tk) support.
echo Delete %VENV_DIR% and retry with a full CPython build that includes tcl/tk.
pause
exit /b 1

:deps_failed
echo ERROR: failed to install build dependencies.
pause
exit /b 1

:build_failed
echo ERROR: build failed.
pause
exit /b 1
