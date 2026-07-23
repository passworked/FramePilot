@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo This mode may change the current SteamVR application's resolutionScale once.
pause
FramePilotVRCLI.exe --wait --apply
pause
