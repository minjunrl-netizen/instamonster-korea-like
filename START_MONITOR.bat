@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   Account Health Monitor
echo   Checks every 3 hours if accounts are alive
echo   Keep this window open. Stop with Ctrl+C.
echo ============================================
echo.
python monitor_scheduler.py
pause
