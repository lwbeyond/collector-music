@echo off
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python.exe" -X utf8 -m collector_music receive-phone
pause
