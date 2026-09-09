@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -X utf8 scripts\manage.py auto
) else (
  py -3 -X utf8 scripts\manage.py auto
)
if errorlevel 1 pause
endlocal
