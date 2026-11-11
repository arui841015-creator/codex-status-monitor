@echo off
rem Install runtime dependencies into the project-local venv. ASCII only.
setlocal
cd /d "%~dp0"

set "VENV_DIR=%~dp0.venv"
set "VENV_PY=%~dp0.venv\Scripts\python.exe"

if exist "%VENV_PY%" goto check_tk

echo Creating isolated venv ...
where py >nul 2>nul
if errorlevel 1 goto venv_by_python
py -3.11 -m venv "%VENV_DIR%"
if exist "%VENV_PY%" goto check_tk

:venv_by_python
python -m venv "%VENV_DIR%"
if not exist "%VENV_PY%" goto venv_failed

:check_tk
"%VENV_PY%" -c "import tkinter" >nul 2>nul
if errorlevel 1 goto no_tkinter

"%VENV_PY%" -m pip install --no-input --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto deps_failed

echo Ready. Run start-monitor.bat to open the monitor.
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
echo ERROR: dependency installation failed.
pause
exit /b 1
