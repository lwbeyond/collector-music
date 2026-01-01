@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements-lock.txt
if errorlevel 1 goto failed
call npm.cmd ci --ignore-scripts --no-audit --no-fund
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -X utf8 -m collector_music init
if errorlevel 1 goto failed
echo 环境已就绪。日常使用打开 00-控制台.cmd。
pause
exit /b 0
:failed
echo 安装未完成，请保留上面的错误信息。
pause
exit /b 1
