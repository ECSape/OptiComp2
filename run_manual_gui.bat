@echo off
rem Self-elevate: the spectrometer USB recovery (pnputil /restart-device) needs an elevated process.
net session >nul 2>&1
if %errorlevel% neq 0 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
cd /d %~dp0
py -X utf8 tools\manual_gui.py
pause
