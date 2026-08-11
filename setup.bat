@echo off
setlocal

echo [setup] Running automated setup...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"

if errorlevel 1 (
  echo.
  echo [setup] Setup failed. See errors above.
  pause
  exit /b 1
)

echo.
echo [setup] Setup finished successfully.
echo [setup] Next steps:
echo [setup]   .venv\Scripts\python.exe scripts\doctor.py
echo [setup]   start.ps1
pause
