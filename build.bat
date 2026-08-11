@echo off
setlocal

echo [build] Building AutoPoster.exe...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1"

if errorlevel 1 (
  echo.
  echo [build] Build failed. See errors above.
  pause
  exit /b 1
)

echo.
echo [build] Build finished successfully.
echo [build] Output: ..\core\AutoPoster.exe
pause
