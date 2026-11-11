@echo off
rem Launch from the project-local venv when present, so no global install is needed.
setlocal
cd /d "%~dp0"

if not exist "%~dp0.venv\Scripts\pythonw.exe" goto fallback
start "" "%~dp0.venv\Scripts\pythonw.exe" monitor_app.py
exit /b 0

:fallback
python monitor_app.py
exit /b 0
