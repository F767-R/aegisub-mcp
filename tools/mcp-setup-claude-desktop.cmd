@echo off
if exist "%~dp0.venv\Scripts\python.exe" (
  call "%~dp0setup-mcp.cmd" --claude-desktop --skip-install %*
) else (
  call "%~dp0setup-mcp.cmd" --claude-desktop %*
)
exit /b %errorlevel%
