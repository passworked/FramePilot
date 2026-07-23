# FramePilot VR

FramePilot VR is the new project identity for the SteamVR Adaptive Resolution
proof of concept. The 0.6.0 controller, desktop panel, persistent SteamVR OSD,
and local VRChat world/population learning were migrated here on 2026-07-23.

## Current scope

- Read OpenVR frame timing and system CPU/GPU telemetry.
- Adjust SteamVR per-application `resolutionScale` against a selected frame budget.
- Render a configurable, persistent in-headset parameter OSD.
- Learn safe local resolution profiles from VRChat world and population context.
- Prepare privacy-preserving aggregate telemetry for a future cross-machine model.

## Compatibility

The Python module names, SteamVR overlay key, Qt settings identity, and
`%LOCALAPPDATA%\SteamVRAdaptiveResolution` storage path intentionally remain
unchanged in 0.6.0. This preserves existing settings, learned profiles, and
prevents a renamed build from creating a duplicate OSD.

## Development

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[build]"
.\.venv\Scripts\python.exe .\steamvr_adaptive_gui.py
```

Build distributable executables with:

```powershell
.\build.ps1
```
