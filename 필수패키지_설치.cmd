@echo off
chcp 65001 >nul
setlocal
set "PYTHON_EXE=%USERPROFILE%\AppData\Local\Python\bin\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"
cd /d "%~dp0"
"%PYTHON_EXE%" -m pip install -r requirements.txt
pause
endlocal
