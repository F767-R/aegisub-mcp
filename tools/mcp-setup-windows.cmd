@echo off
cd /d "%~dp0"
python setup-mcp.py
if errorlevel 1 (
  echo Setup failed. Install Python 3.11 or newer and ensure python is on PATH.
  pause
  exit /b 1
)
pause
