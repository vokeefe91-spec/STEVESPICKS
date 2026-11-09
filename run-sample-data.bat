@echo off
cd /d "%~dp0"
set MOCK=1
start "" http://localhost:8080
python server.py
pause
