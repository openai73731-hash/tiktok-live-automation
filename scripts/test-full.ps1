# Tier C: explicit milestone/release/merge regression only; never called by fast/pc.
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $repoRoot
try {
    & './.venv/Scripts/python.exe' -m pytest -q
    exit $LASTEXITCODE
} finally { Pop-Location }
