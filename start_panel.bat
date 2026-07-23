@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "FramePilotVR.exe" (
  start "" "FramePilotVR.exe"
  exit /b 0
)
if exist "FramePilotVR\FramePilotVR.exe" (
  start "" "FramePilotVR\FramePilotVR.exe"
  exit /b 0
)
if exist "dist\FramePilotVR\FramePilotVR.exe" (
  start "" "dist\FramePilotVR\FramePilotVR.exe"
  exit /b 0
)
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" "steamvr_adaptive_gui.py"
  exit /b 0
)
python steamvr_adaptive_gui.py
