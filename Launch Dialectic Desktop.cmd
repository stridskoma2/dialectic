@echo off
setlocal

set "repositoryRoot=%~dp0"
set "python=%repositoryRoot%.venv\Scripts\python.exe"
set "pythonw=%repositoryRoot%.venv\Scripts\pythonw.exe"

if exist "%python%" if exist "%pythonw%" (
    "%python%" -c "import PySide6" >nul 2>&1
    if not errorlevel 1 (
        start "" /D "%repositoryRoot%" "%pythonw%" -m dialectic.desktop
        exit /b 0
    )
)

where.exe dialectic-desktop.exe >nul 2>&1
if not errorlevel 1 (
    start "" /D "%repositoryRoot%" dialectic-desktop.exe
    exit /b 0
)

echo Dialectic's native desktop UI is not installed.
echo From this repository, run: .venv\Scripts\python.exe -m pip install -e ".[desktop]"
pause
exit /b 1
