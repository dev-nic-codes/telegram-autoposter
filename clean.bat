@echo off
setlocal

echo [clean] Cleaning data and temp files...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0clean.ps1"

if errorlevel 1 (
  echo.
  echo [clean] Clean failed. See errors above.
  pause
  exit /b 1
)

echo.
echo [clean] Clean finished successfully.
pause
