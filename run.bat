@echo off
setlocal
python --version >nul 2>&1
if errorlevel 1 (
  echo Python 3.10 or newer is required. Install Python and try again.
  pause
  exit /b 1
)
python run.py
pause
