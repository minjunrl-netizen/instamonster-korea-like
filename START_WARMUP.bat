@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   Auto Warmup Scheduler
echo   Runs daily at a random time (9am-10pm)
echo   Keep this window open. Stop with Ctrl+C.
echo ============================================
echo.
python warmup_scheduler.py
pause
