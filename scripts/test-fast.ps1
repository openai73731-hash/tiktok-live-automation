# Tier A: pass the files/selectors covering your edit. Default = offline PC contracts.
param([string[]]$TestPaths = @('backend/tests/test_windows_live_studio.py'))
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $repoRoot
try {
    & './.venv/Scripts/python.exe' -m pytest -q @TestPaths
    exit $LASTEXITCODE
} finally { Pop-Location }
