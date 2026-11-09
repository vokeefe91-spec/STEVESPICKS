@echo off
title Steve's Picks (NEW) - keep this window open
cd /d "%~dp0"
set PORT=8095
where python >nul 2>nul || (echo Python is not installed. Get it from python.org and tick "Add Python to PATH". & pause & exit /b)
echo.
echo  Keep this window open while you use Steve's Picks.
echo  Do NOT click inside this window - Windows pauses the app when you do.
echo  (If it ever freezes, click here and press Enter.)
echo.
start "" http://localhost:8095
python -u server.py
pause
