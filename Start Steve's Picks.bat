@echo off
title Steve's Picks
cd /d "%~dp0"
echo Starting Steve's Picks at http://localhost:8093 ...
start "" http://localhost:8093
python app.py
pause
