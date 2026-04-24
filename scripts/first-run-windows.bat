@echo off
setlocal
cd /d "%~dp0.."

set "POWERSHELL_EXE="
where powershell >nul 2>nul
if not errorlevel 1 set "POWERSHELL_EXE=powershell"

if not defined POWERSHELL_EXE (
  echo first-run-windows.bat: 未找到 PowerShell。
  pause
  exit /b 1
)

"%POWERSHELL_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0first-run-windows.ps1"
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
