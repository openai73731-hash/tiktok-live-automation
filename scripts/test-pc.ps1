# Tier B: both transports, listener, policy/approval, manual/AUTO queues and Admin.
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $repoRoot
try {
    & './.venv/Scripts/python.exe' -m pytest -q backend/tests/test_unified_transports.py backend/tests/test_mobile_acceptance.py backend/tests/test_guarded_delivery.py backend/tests/test_tiktok_you.py backend/tests/test_windows_live_studio.py backend/tests/test_windows_native_discovery.py backend/tests/test_windows_sender.py backend/tests/test_windows_hardening.py backend/tests/test_windows_release_hardening.py backend/tests/test_acceptance_launcher.py backend/tests/test_config_models.py backend/tests/test_logging.py backend/tests/test_service.py backend/tests/test_listener.py backend/tests/test_android_delivery.py backend/tests/test_manual_send_rc.py backend/tests/test_predelivery_hardening.py backend/tests/test_admin.py backend/tests/test_operator_runtime.py backend/tests/test_ai.py
    exit $LASTEXITCODE
} finally { Pop-Location }
