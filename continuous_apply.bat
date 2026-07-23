@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo WARNING: Use only after apply_one_step.bat proves that the game updates resolution while running.
pause
FramePilotVRCLI.exe --wait --apply --continuous-apply
pause
