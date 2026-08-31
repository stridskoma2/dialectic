@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Launch Dialectic UI.ps1"
if errorlevel 1 pause
