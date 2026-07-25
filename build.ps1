param(
    [string]$DistPath = ''
)

$ErrorActionPreference = 'Stop'

$sourceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvDir = Join-Path $sourceDir '.venv'
$python = Join-Path $venvDir 'Scripts\python.exe'
$appIcon = Join-Path $sourceDir 'assets\framepilot-vr.ico'
$appIconPng = Join-Path $sourceDir 'assets\framepilot-vr-icon.png'
$distDir = if ($DistPath) {
    [IO.Path]::GetFullPath((Join-Path $sourceDir $DistPath))
} else {
    Join-Path $sourceDir 'dist'
}

if (-not (Test-Path -LiteralPath $python)) {
    python -m venv $venvDir
}

& $python -m pip install --disable-pip-version-check -r (Join-Path $sourceDir 'requirements.txt')
$openvrDll = & $python -c "import pathlib, openvr; print(pathlib.Path(openvr.__file__).parent / 'libopenvr_api_64.dll')"

& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --console `
    --name FramePilotVRCLI `
    --icon $appIcon `
    --distpath $distDir `
    --workpath (Join-Path $sourceDir 'build') `
    --specpath $sourceDir `
    --add-binary "$openvrDll;openvr" `
    (Join-Path $sourceDir 'steamvr_adaptive_poc.py')

& (Join-Path $distDir 'FramePilotVRCLI\FramePilotVRCLI.exe') --self-test

& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --windowed `
    --name FramePilotVR `
    --icon $appIcon `
    --distpath $distDir `
    --workpath (Join-Path $sourceDir 'build-panel') `
    --specpath $sourceDir `
    --add-binary "$openvrDll;openvr" `
    --add-data "$appIconPng;assets" `
    --add-data "$(Join-Path $sourceDir 'locales');locales" `
    (Join-Path $sourceDir 'steamvr_adaptive_gui.py')
