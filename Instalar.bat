@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto erro
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto erro
".venv\Scripts\python.exe" -m invisible_playwright fetch
if errorlevel 1 goto erro
echo.
echo Instalacao concluida. Abra Iniciar.bat.
pause
exit /b 0
:erro
echo.
echo Falha na instalacao. Verifique Python 3.11 ou superior e a conexao.
pause
exit /b 1
