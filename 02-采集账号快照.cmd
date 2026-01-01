@echo off
chcp 65001 >nul
cd /d "%~dp0"
".venv\Scripts\python.exe" -X utf8 -m collector_music collect
if errorlevel 3 goto done
if errorlevel 2 goto publish
if errorlevel 1 goto done
:publish
".venv\Scripts\python.exe" -X utf8 -m collector_music publish
:done
pause
