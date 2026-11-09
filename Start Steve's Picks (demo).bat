@echo off
title Steve's Picks (demo)
cd /d "%~dp0"
set DEMO=1
echo Starting Steve's Picks DEMO at http://localhost:8093 ...
start "" http://localhost:8093
python app.py
pause
