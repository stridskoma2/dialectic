@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" -m dialectic.ui
  exit /b 0
)

where dialectic-ui.exe >nul 2>nul
if not errorlevel 1 (
  start "" dialectic-ui.exe
  exit /b 0
)

echo Dialectic is not installed in .venv.
echo Follow the README install steps, then double-click this launcher again.
pause
