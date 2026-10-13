# Tier A: unified switching, Android guarded delivery and all offline PC contracts.
param([string[]]$TestPaths = @('backend/tests/test_unified_transports.py', 'backend/tests/test_mobile_acceptance.py', 'backend/tests/test_guarded_delivery.py', 'backend/tests/test_windows_live_studio.py', 'backend/tests/test_windows_native_discovery.py', 'backend/tests/test_windows_sender.py', 'backend/tests/test_windows_hardening.py', 'backend/tests/test_windows_release_hardening.py', 'backend/tests/test_acceptance_launcher.py'))
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $repoRoot
try {
    & './.venv/Scripts/python.exe' -m pytest -q @TestPaths
    exit $LASTEXITCODE
} finally { Pop-Location }
