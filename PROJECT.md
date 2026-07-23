# FramePilot VR

FramePilot VR is the new project identity for the SteamVR Adaptive Resolution
proof of concept. The 0.7.x controller, desktop panel, persistent SteamVR OSD,
and local VRChat world/population learning were migrated here on 2026-07-23.

## Current scope

- Read OpenVR frame timing and system CPU/GPU telemetry.
- Adjust SteamVR per-application `resolutionScale` against a selected frame budget.
- Render a configurable, persistent in-headset parameter OSD.
- Learn safe local resolution profiles from VRChat world and population context.
- Passively collect privacy-preserving steady-load and population-transition
  aggregates during normal VRChat play, with local ZIP export, confirmed
  manual upload, or Guide-authorized automatic incremental upload through the
  Cloudflare Worker sharing gateway.

## Compatibility

The Python module names, SteamVR overlay key, Qt settings identity, and
`%LOCALAPPDATA%\SteamVRAdaptiveResolution` storage path intentionally remain
unchanged throughout 0.7.x. This preserves existing settings, learned profiles, and
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

## Version policy

FramePilot VR uses `MAJOR.MINOR.PATCH`. The major version changes only when the
user explicitly requests it. New capabilities increase the minor version;
ordinary fixes, maintenance, and UI adjustments without a clear new
capability increase the patch version. Documentation-only and constraint-only
changes do not require a release bump. `pyproject.toml` is canonical, and all
visible version strings and artifact filenames must be synchronized before a
release is delivered.
