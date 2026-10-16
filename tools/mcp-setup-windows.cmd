@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" setup-mcp.py %*
) else (
  python setup-mcp.py %*
)
if errorlevel 1 (
  echo Setup failed. See the error above. Initial setup requires Python 3.11 or newer on PATH.
  pause
  exit /b 1
)
pause
