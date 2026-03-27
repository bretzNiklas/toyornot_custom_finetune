$ErrorActionPreference = "Stop"

$workspaceRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $workspaceRoot ".venv\Scripts\python.exe"
$labelStudioExe = Join-Path $workspaceRoot ".venv\Scripts\label-studio.exe"
$baseDataDir = Join-Path $workspaceRoot "label_studio\.data"

if (-not (Test-Path $venvPython)) {
    throw "Virtualenv not found at $venvPython"
}

if (-not (Test-Path $labelStudioExe)) {
    throw "Label Studio executable not found at $labelStudioExe"
}

New-Item -ItemType Directory -Force -Path $baseDataDir | Out-Null

$env:LABEL_STUDIO_BASE_DATA_DIR = $baseDataDir
$env:LABEL_STUDIO_LOCAL_FILES_SERVING_ENABLED = "true"
$env:LABEL_STUDIO_LOCAL_FILES_DOCUMENT_ROOT = $workspaceRoot
$env:NO_COLOR = "1"

Write-Host "Using workspace root: $workspaceRoot"
Write-Host "Using Label Studio data dir: $baseDataDir"
Write-Host "Local files root: $env:LABEL_STUDIO_LOCAL_FILES_DOCUMENT_ROOT"
Write-Host "Starting Label Studio on http://localhost:8080 ..."

& $labelStudioExe start --port 8080
