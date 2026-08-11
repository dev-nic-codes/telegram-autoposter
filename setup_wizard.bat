@echo off
setlocal

echo [setup_wizard] Launching setup wizard...

where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%~dp0setup_wizard.py"
) else (
  python "%~dp0setup_wizard.py"
)

if errorlevel 1 (
  echo.
  echo [setup_wizard] The wizard exited with an error.
  pause
  exit /b 1
)

echo.
echo [setup_wizard] Setup wizard finished.
pause
