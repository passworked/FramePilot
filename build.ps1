$ErrorActionPreference = 'Stop'

$sourceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvDir = Join-Path $sourceDir '.venv'
$python = Join-Path $venvDir 'Scripts\python.exe'

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
    --distpath (Join-Path $sourceDir 'dist') `
    --workpath (Join-Path $sourceDir 'build') `
    --specpath $sourceDir `
    --add-binary "$openvrDll;openvr" `
    (Join-Path $sourceDir 'steamvr_adaptive_poc.py')

& (Join-Path $sourceDir 'dist\FramePilotVRCLI\FramePilotVRCLI.exe') --self-test

& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --windowed `
    --name FramePilotVR `
    --distpath (Join-Path $sourceDir 'dist') `
    --workpath (Join-Path $sourceDir 'build-panel') `
    --specpath $sourceDir `
    --add-binary "$openvrDll;openvr" `
    (Join-Path $sourceDir 'steamvr_adaptive_gui.py')
