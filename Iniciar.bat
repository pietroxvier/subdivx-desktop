@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    call Instalar.bat
    if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" main.py
if errorlevel 1 pause
