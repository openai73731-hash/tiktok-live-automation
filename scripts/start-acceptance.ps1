# Prepare the ONE next operator acceptance. Never connects, arms, fills or sends.
[CmdletBinding()]
param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $repoRoot '.venv\Scripts\python.exe'
$studioExe = 'C:\Program Files\TikTok LIVE Studio\1.36.6\TikTok LIVE Studio.exe'
$adminUrl = 'http://127.0.0.1:8000'
$launchLock = $null
$startedBackend = $false
$backendOwner = $null

# Studio's installed manifest requests highestAvailable. Use the normal UAC
# consent flow; a medium-integrity backend cannot send input to elevated Studio.
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'Approve the standard Windows UAC prompt to match elevated Studio. No input or LIVE is started.'
    $elevationArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $PSCommandPath + '"'))
    if ($NoBrowser) { $elevationArgs += '-NoBrowser' }
    $elevated = Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') -ArgumentList $elevationArgs -Verb RunAs -WindowStyle Hidden -Wait -PassThru
    if ($elevated.ExitCode -ne 0) { throw 'Elevated preparation failed. See work/pc/logs/last-launch-error.txt.' }
    Write-Host 'Elevated acceptance preparation complete. AUTO OFF; listener disconnected.'
    exit 0
}
Push-Location -LiteralPath $repoRoot

function Get-Listeners([int[]]$Ports) {
    @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalPort -in $Ports } |
        Select-Object LocalAddress, LocalPort, OwningProcess |
        Sort-Object LocalPort, LocalAddress, OwningProcess)
}

function Get-Admin([string]$Path) {
    Invoke-RestMethod -Uri "$adminUrl$Path" -TimeoutSec 10
}

function Assert-Idle($Status, $Delivery) {
    if ($Status.live.enabled -or $Status.live.connection_owned -or $Status.live.live_session_id -or
        $Status.live.state -notin @('stopped', 'disconnected')) {
        throw 'Listener is active. STOP / DISCONNECT in Admin before preparation.'
    }
    if ($Delivery.transport -ne 'windows_live_studio' -or $Delivery.busy -or
        $Delivery.windows_sender.auto_enabled -or $Delivery.windows_sender.auto_epoch_state -ne 'OFF' -or
        $Delivery.windows_sender.state -notin @('DISARMED', 'NEED_OPERATOR') -or
        $Delivery.windows_sender.watch_ready -or $null -ne $Delivery.windows_sender.armed_age_seconds) {
        throw 'Sender is active or not the Windows transport. STOP / DISARM before preparation.'
    }
    if ($Delivery.windows_sender.acceptance_status -eq 'SPENT' -or
        $Delivery.windows_sender.empty_after_verified_confirmed -or
        $Delivery.windows_sender.last_send_status -in @('FILLED_ONLY', 'SENT_CONFIRMED')) {
        throw 'Acceptance has already progressed. Do not restart to repeat a phase.'
    }
}

function Assert-BackendOwner([int]$OwnerId) {
    # A venv redirector on Windows launches a second, base-Python process.
    # Validate both command lines and the exact venv ancestor before stopping either.
    $currentId = $OwnerId
    for ($depth = 0; $depth -lt 3; $depth++) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $currentId"
        if (-not $process -or $process.CommandLine -notmatch
            '(?i) -m uvicorn app\.main:app --app-dir backend --host 127\.0\.0\.1 --port 8000\s*$') {
            throw 'Port 8000 is not the recognized repo backend; refusing to stop it.'
        }
        if ($process.ExecutablePath -eq $pythonExe) { return }
        $currentId = [int]$process.ParentProcessId
    }
    throw 'Port 8000 has no matching repository venv ancestor.'
}

try {
    if (-not (Test-Path -LiteralPath $pythonExe) -or -not (Test-Path -LiteralPath $studioExe)) {
        throw 'The repository venv or expected Studio executable is missing.'
    }
    New-Item -ItemType Directory -Path 'work/pc/logs', 'work/pc/data' -Force | Out-Null
    $launchLock = [IO.File]::Open((Join-Path $repoRoot 'work/pc/acceptance-launch.lock'),
        [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    $runId = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $runDir = Join-Path $repoRoot "work/pc/logs/launch-$runId"
    New-Item -ItemType Directory -Path $runDir | Out-Null
    $receiptPath = Join-Path $repoRoot 'work/pc/acceptance-launch.json'
    $branch = & git branch --show-current
    if ($LASTEXITCODE -ne 0 -or $branch -ne 'feature/windows-live-studio') {
        throw 'Use branch feature/windows-live-studio.'
    }
    $head = & git rev-parse HEAD
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read repository HEAD.' }
    $gitStatus = @(& git status --porcelain)
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read git status.' }
    if ($gitStatus.Count) { Write-Warning 'Repository has local changes; no commit or push is performed.' }
    Write-Host "Repository: $repoRoot | $branch | $head"

    $pocketBefore = ConvertTo-Json -InputObject @(Get-Listeners @(9000, 9002, 9003)) -Compress
    $listeners = @(Get-Listeners @(8000))
    if ($listeners.Count) {
        if ($listeners.Count -ne 1 -or $listeners[0].LocalAddress -ne '127.0.0.1') {
            throw 'Port 8000 must have one loopback listener.'
        }
        $backendOwner = [int]$listeners[0].OwningProcess
        Assert-BackendOwner $backendOwner
        Assert-Idle (Get-Admin '/api/admin/status') (Get-Admin '/api/admin/delivery/status')
    }

    $studioGui = @(Get-Process -Name 'TikTok LIVE Studio' -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowHandle -ne 0 })
    if ($studioGui.Count -ne 1) { throw 'Open exactly one Studio GUI manually; no LIVE is started by this script.' }
    $studioId = [int]$studioGui[0].Id
    $startup = @{
        TIKTOK_TRANSPORT = 'windows_live_studio'; TIKTOK_USERNAME = 'd3pp1s'
        WINDOWS__EXECUTABLE_PATH = $studioExe; WINDOWS__PROCESS_ID = [string]$studioId
        WINDOWS__BROADCASTER_USER_ID = '7412662672502555649'
        WINDOWS__MANUAL_FILL_ENABLED = 'true'; WINDOWS__ACCEPTANCE_ONE_SHOT = 'true'
        WINDOWS__ARM_TTL_SECONDS = '600'; WINDOWS__FOCUS_WATCH_SECONDS = '15'
        WINDOWS__VERIFICATION_SECONDS = '10'; WINDOWS__COOLDOWN_SECONDS = '3'
        ENABLE_PHYSICAL_WINDOWS_SEND = 'true'; ENABLE_PHYSICAL_TIKTOK_SEND = 'false'
        ADMIN_LIVE_ENABLED = 'false'; LIVE_MODE = 'real'; DIAGNOSTIC = 'false'
        STATE_DIR = 'work/pc/data'; LOG_DIR = 'work/pc/logs'
        PROJECT_CONFIG_PATH = 'config/project.lmstudio.yaml'
        KNOWLEDGE_PATH = 'config/bot_knowledge.yaml'; FAKE_REPEAT = 'false'
        PYTHONUTF8 = '1'; PYTHONIOENCODING = 'utf-8'
    }
    foreach ($key in $startup.Keys) { [Environment]::SetEnvironmentVariable($key, $startup[$key], 'Process') }
    Remove-Item Env:REPLAY_SCENARIO -ErrorAction SilentlyContinue

    # Limited process-image query only: no accessibility, focus hooks or input API.
    $preflightCode = @'
import ctypes, json, sqlite3, sys
from ctypes import wintypes as w
from pathlib import Path
from app.config import Settings
from app.ai.knowledge import ProjectConfig, load_yaml_model
from app.admin.operator_config import OperatorStore
s = Settings()
k = ctypes.WinDLL('kernel32', use_last_error=True)
k.OpenProcess.argtypes = (w.DWORD, w.BOOL, w.DWORD); k.OpenProcess.restype = w.HANDLE
k.QueryFullProcessImageNameW.argtypes = (w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD))
k.QueryFullProcessImageNameW.restype = w.BOOL
k.CloseHandle.argtypes = (w.HANDLE,)
h = k.OpenProcess(0x1000, False, s.windows.process_id)
if not h: raise SystemExit('Cannot query Studio process identity.')
try:
    image = ctypes.create_unicode_buffer(32768); length = w.DWORD(len(image))
    if not k.QueryFullProcessImageNameW(h, 0, image, ctypes.byref(length)):
        raise SystemExit('Cannot read Studio executable path.')
    if Path(image.value).resolve() != s.windows.executable_path.resolve():
        raise SystemExit('Studio executable path mismatch.')
finally: k.CloseHandle(h)
a = ctypes.WinDLL('advapi32', use_last_error=True)
a.OpenProcessToken.argtypes = (w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)); a.OpenProcessToken.restype = w.BOOL
a.GetTokenInformation.argtypes = (w.HANDLE, w.DWORD, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD))
a.GetTokenInformation.restype = w.BOOL
a.GetSidSubAuthorityCount.argtypes = (w.LPVOID,); a.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
a.GetSidSubAuthority.argtypes = (w.LPVOID, w.DWORD); a.GetSidSubAuthority.restype = ctypes.POINTER(w.DWORD)
class Label(ctypes.Structure):
    _fields_ = [('sid', w.LPVOID), ('attributes', w.DWORD)]
def integrity(pid):
    p = k.OpenProcess(0x1000, False, pid); token = w.HANDLE()
    try:
        if not p or not a.OpenProcessToken(p, 8, ctypes.byref(token)):
            raise SystemExit('Cannot read process integrity; approve normal UAC elevation.')
        size = w.DWORD(); a.GetTokenInformation(token, 25, None, 0, ctypes.byref(size))
        data = ctypes.create_string_buffer(size.value)
        if not a.GetTokenInformation(token, 25, data, len(data), ctypes.byref(size)):
            raise SystemExit('Cannot verify process integrity.')
        sid = ctypes.cast(data, ctypes.POINTER(Label)).contents.sid
        count = a.GetSidSubAuthorityCount(sid).contents.value
        return a.GetSidSubAuthority(sid, count - 1).contents.value
    finally:
        if token: k.CloseHandle(token)
        if p: k.CloseHandle(p)
import os
studio_integrity = integrity(s.windows.process_id)
launcher_integrity = integrity(os.getpid())
backend_integrity = integrity(int(sys.argv[1])) if len(sys.argv) > 1 else None
if launcher_integrity < studio_integrity or (backend_integrity is not None and backend_integrity < studio_integrity):
    raise SystemExit('Backend integrity is below Studio; Windows UIPI would block input.')
base = load_yaml_model(s.project_config_path, ProjectConfig)
store = OperatorStore(base, s.project_config_path.parent / 'operator.local.yaml')
overlay = store.load(); project = store.validate(overlay)
own = overlay.tiktok.username if overlay.tiktok.username is not None else s.tiktok_username
if own != 'd3pp1s': raise SystemExit('Configured own account must be d3pp1s; correct the local settings.')
if (project.ai.provider, project.ai.model, project.ai.lmstudio_base_url, project.ai.thinking_enabled) != (
        'lmstudio', 'qwen/qwen3-8b', 'http://127.0.0.1:1234/v1', False):
    raise SystemExit('Configured local AI differs from the acceptance profile.')
counts = []
journal = s.state_dir / 'windows-delivery.sqlite3'
if journal.exists():
    c = sqlite3.connect(journal.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        if c.execute('pragma quick_check').fetchone()[0] != 'ok': raise SystemExit('Journal check failed.')
        counts = [dict(status=r[0], mode=r[1], count=r[2]) for r in c.execute(
            'select status, mode, count(*) from operations group by status, mode')]
    finally: c.close()
if any(r['status'] == 'reserved' or r['mode'] == 'AUTO' for r in counts):
    raise SystemExit('Existing reserved/AUTO operation: STOP; do not reset or repeat acceptance.')
print(json.dumps(dict(cwd=str(Path.cwd()), studio_pid=s.windows.process_id, studio_path=image.value,
    studio_integrity_rid=studio_integrity, launcher_integrity_rid=launcher_integrity,
    backend_integrity_rid=backend_integrity, own_account=own,
    windows=s.windows.model_dump(mode='json'), journal=counts)))
'@
    $preflightJson = @($preflightCode | & $pythonExe -)
    if ($LASTEXITCODE -ne 0) { throw 'Read-only acceptance preflight failed.' }
    $preflight = ($preflightJson -join "`n") | ConvertFrom-Json
    if ($preflight.cwd -ne $repoRoot) { throw 'Backend must launch from the repository root.' }
    if (@($preflight.journal | Where-Object { $_.status -in @('unknown', 'needs_operator') }).Count) {
        Write-Warning 'Historical MANUAL UNKNOWN records preserved. Never retry those replies; use fresh comments only.'
    }
    Write-Host "Studio GUI PID: $studioId | executable verified | own account: d3pp1s"

    $lmsExe = (Get-Command lms.exe -ErrorAction Stop).Source
    & $lmsExe daemon up
    if ($LASTEXITCODE -ne 0) { throw 'Cannot start llmster.' }
    $server = (& $lmsExe server status --json) | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect LM Studio server.' }
    if (-not $server.running) {
        & $lmsExe server start --port 1234 --bind 127.0.0.1
        if ($LASTEXITCODE -ne 0) { throw 'Cannot start local LM Studio server.' }
    } elseif ($server.port -ne 1234) { throw 'LM Studio uses another port; refusing to change it.' }
    $lmListeners = @(Get-Listeners @(1234))
    if (-not $lmListeners.Count -or @($lmListeners | Where-Object { $_.LocalAddress -notin @('127.0.0.1', '::1') }).Count) {
        throw 'LM Studio must listen only on loopback port 1234.'
    }
    $loaded = @((& $lmsExe ps --json) | ConvertFrom-Json)
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect loaded models.' }
    if (-not @($loaded | Where-Object { $_.identifier -eq 'qwen/qwen3-8b' }).Count) {
        & $lmsExe load qwen/qwen3-8b --identifier qwen/qwen3-8b --context-length 8192 --yes
        if ($LASTEXITCODE -ne 0) { throw 'Cannot load the installed Qwen model.' }
    }
    $models = Invoke-RestMethod 'http://127.0.0.1:1234/v1/models' -TimeoutSec 15
    if ('qwen/qwen3-8b' -notin @($models.data.id)) { throw 'Qwen identifier missing from /v1/models.' }
    $body = @{ model = 'qwen/qwen3-8b'; temperature = 0; max_tokens = 16; stream = $false
        messages = @(@{ role = 'user'; content = 'Reply only OK. /no_think' }) } | ConvertTo-Json -Depth 5
    $inference = Invoke-RestMethod 'http://127.0.0.1:1234/v1/chat/completions' -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 90
    if ($inference.choices[0].message.content.Trim() -notmatch '^OK[.!]?$' -or $inference.choices[0].finish_reason -ne 'stop') {
        throw 'Short /no_think inference failed.'
    }

    $env:LOG_DIR = Join-Path $runDir 'smoke'
    try {
        & $pythonExe -m app.main --project-config config/project.lmstudio.yaml --ai-smoke-provider lmstudio 'Is bot trading automatically?'
        if ($LASTEXITCODE -ne 0) { throw 'Application LM Studio smoke failed.' }
        $smoke = @(Get-Content -LiteralPath (Join-Path $env:LOG_DIR 'app.jsonl') |
            ForEach-Object { $_ | ConvertFrom-Json } | Where-Object { $_.event -eq 'ai_smoke_result' })
        if ($smoke.Count -ne 1 -or $smoke[0].suggestion.status -ne 'ready') {
            throw 'Application LM Studio smoke did not return status=ready.'
        }
    } finally { $env:LOG_DIR = 'work/pc/logs' }

    $reuse = $false
    if ($backendOwner) {
        if (Test-Path -LiteralPath $receiptPath) {
            $previous = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
            $ownerProcess = Get-Process -Id $backendOwner
            $reuse = ($previous.backend_pid -eq $backendOwner -and $previous.head -eq $head -and
                $previous.preflight.PSObject.Properties['backend_integrity_rid'] -and
                $previous.preflight.backend_integrity_rid -ge $preflight.studio_integrity_rid -and
                $previous.backend_started_utc -eq $ownerProcess.StartTime.ToUniversalTime().ToString('o') -and
                $previous.studio_pid -eq $studioId -and $previous.studio_started_utc -eq $studioGui[0].StartTime.ToUniversalTime().ToString('o'))
        }
        # Recheck after inference; an operator may have connected in the meantime.
        Assert-Idle (Get-Admin '/api/admin/status') (Get-Admin '/api/admin/delivery/status')
        if (-not $reuse) {
            Assert-BackendOwner $backendOwner
            Stop-Process -Id $backendOwner -ErrorAction Stop
            $deadline = (Get-Date).AddSeconds(15)
            while (@(Get-Listeners @(8000)).Count) {
                if ((Get-Date) -gt $deadline) { throw 'Old backend did not release port 8000.' }
                Start-Sleep -Milliseconds 250
            }
        }
    }
    if (-not $reuse) {
        $backendProcess = Start-Process -FilePath $pythonExe -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--app-dir', 'backend', '--host', '127.0.0.1', '--port', '8000') -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runDir 'backend.stdout.log') -RedirectStandardError (Join-Path $runDir 'backend.stderr.log')
        $startedBackend = $true
        $deadline = (Get-Date).AddSeconds(30)
        while ($true) {
            try { $status = Get-Admin '/api/admin/status'; break } catch {
                if ((Get-Date) -gt $deadline -or $backendProcess.HasExited) { throw 'Backend startup failed. Inspect launch logs.' }
                Start-Sleep -Milliseconds 500
            }
        }
    }
    $listeners = @(Get-Listeners @(8000))
    if ($listeners.Count -ne 1 -or $listeners[0].LocalAddress -ne '127.0.0.1') { throw 'Unexpected backend binding.' }
    $backendOwner = [int]$listeners[0].OwningProcess
    Assert-BackendOwner $backendOwner
    $status = Get-Admin '/api/admin/status'
    $delivery = Get-Admin '/api/admin/delivery/status'
    $operator = Get-Admin '/api/admin/operator/settings'
    Assert-Idle $status $delivery
    if ($operator.username -ne 'd3pp1s' -or $operator.saved_username -ne 'd3pp1s' -or
        $operator.pending -or $operator.config_error -or -not $operator.connect_allowed -or
        $operator.active.provider -ne 'lmstudio' -or $operator.active.model -ne 'qwen/qwen3-8b') {
        throw 'Admin own account or active runtime does not match the acceptance profile.'
    }
    if (-not $delivery.physical_delivery_enabled -or -not $delivery.windows_sender.acceptance_one_shot -or
        $delivery.windows_sender.acceptance_status -ne 'AVAILABLE' -or $delivery.windows_sender.watch_seconds -ne 15) {
        throw 'Enter startup gate or one-shot profile mismatch.'
    }
    $session = Get-Admin '/api/admin/session'
    $ai = Invoke-RestMethod "$adminUrl/api/admin/ai/check" -Method Post -ContentType 'application/json' -Headers @{'X-Admin-CSRF' = $session.csrf_token; Origin = $adminUrl} -Body '{}' -TimeoutSec 30
    if (-not $ai.available -or -not $ai.model_found -or $ai.error_category) { throw 'Admin local AI check failed.' }
    $pocketAfter = ConvertTo-Json -InputObject @(Get-Listeners @(9000, 9002, 9003)) -Compress
    if ($pocketAfter -ne $pocketBefore) { throw 'PocketWin listeners changed during preparation; inspect before LIVE.' }
    $freshGui = @(Get-Process -Name 'TikTok LIVE Studio' | Where-Object { $_.MainWindowHandle -ne 0 })
    if ($freshGui.Count -ne 1 -or $freshGui[0].Id -ne $studioId -or $freshGui[0].StartTime -ne $studioGui[0].StartTime) {
        throw 'Studio process changed during preparation; run preparation again before LIVE.'
    }
    $finalPreflight = @($preflightCode | & $pythonExe - $backendOwner)
    if ($LASTEXITCODE -ne 0) { throw 'Final backend/Studio integrity or journal check failed.' }
    $preflight = ($finalPreflight -join "`n") | ConvertFrom-Json
    Assert-Idle (Get-Admin '/api/admin/status') (Get-Admin '/api/admin/delivery/status')
    $receipt = [ordered]@{ prepared_utc = [DateTime]::UtcNow.ToString('o'); repo_root = $repoRoot; head = $head
        backend_pid = $backendOwner; backend_started_utc = (Get-Process -Id $backendOwner).StartTime.ToUniversalTime().ToString('o')
        studio_pid = $studioId; studio_started_utc = $studioGui[0].StartTime.ToUniversalTime().ToString('o')
        preflight = $preflight; application_smoke = $smoke[0].suggestion.status
        admin_ai = $ai; delivery = $delivery; pocketwin_listeners = @(Get-Listeners @(9000, 9002, 9003)) }
    $receipt | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $runDir 'preflight.json') -Encoding UTF8
    $receipt | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $receiptPath -Encoding UTF8
    Write-Host "PRE-LIVE PREPARED | backend PID $backendOwner | Studio PID $studioId | @d3pp1s"
    Write-Host 'Application smoke READY | AUTO OFF | one-shot AVAILABLE | physical Enter gate ON'
    Write-Host 'Windows focus watch NOT READY: only the operator starts ARM during acceptance.'
    Write-Host "Studio/backend integrity verified: $($preflight.studio_integrity_rid)/$($preflight.backend_integrity_rid)"
    Write-Host "Admin: $adminUrl/ui/ | Evidence: $runDir"
    if (-not $NoBrowser) { Start-Process "$adminUrl/ui/" }
} catch {
    # Never stop a reused server or any unrelated process on a failed check.
    if ($startedBackend) {
        try {
            $failedListeners = @(Get-Listeners @(8000))
            foreach ($listener in $failedListeners) {
                Assert-BackendOwner ([int]$listener.OwningProcess)
                Stop-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
            }
            if (-not $backendProcess.HasExited) { Stop-Process -Id $backendProcess.Id -ErrorAction SilentlyContinue }
        } catch { }
    }
    $_.Exception.Message | Set-Content -LiteralPath (Join-Path $repoRoot 'work/pc/logs/last-launch-error.txt') -Encoding UTF8
    Write-Error $_
    exit 1
} finally {
    if ($launchLock) { $launchLock.Dispose() }
    Pop-Location
}
