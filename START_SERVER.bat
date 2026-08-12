@echo off
cd /d "%~dp0"
chcp 65001 >nul

echo ============================================
echo   InstaMonster Server
echo ============================================
echo.
echo   Browser URL:  http://localhost:8000
echo.
echo   * Browser opens automatically in a few seconds.
echo   * If not, type the URL above manually.
echo   * Do NOT close this window (server stops).
echo ============================================
echo.

set "PY=python"
where python >nul 2>nul || set "PY=py"

start "" cmd /c "timeout /t 6 >nul & start http://localhost:8000"

%PY% server.py

echo.
echo Server stopped.
pause
