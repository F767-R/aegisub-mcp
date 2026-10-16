@echo off
if not exist "%~dp0mcp-config.json" (
  echo Run setup-mcp.cmd in the portable folder first.
  pause
  exit /b 1
)
where claude >nul 2>nul
if errorlevel 1 (
  echo Install Claude Code and make the claude command available on PATH first.
  pause
  exit /b 1
)
rem Preserve the caller's project directory; load Aegisub only for this session.
call claude --mcp-config "%~dp0mcp-config.json" %*
exit /b %errorlevel%
