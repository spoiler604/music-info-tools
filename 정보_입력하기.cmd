@echo off
chcp 65001 >nul
setlocal
set "PYTHON_EXE=%USERPROFILE%\AppData\Local\Python\bin\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"
cd /d "%~dp0"
"%PYTHON_EXE%" -B apply_music_info.py --gui
if errorlevel 1 pause
endlocal
