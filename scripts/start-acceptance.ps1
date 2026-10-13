# Prepare the ONE next operator acceptance. Never connects, arms, fills or sends.
[CmdletBinding()]
param([switch]$NoBrowser, [string]$PreparationLaunchId,
    [ValidateRange(30, 900)][int]$PreparationTimeoutSeconds = 300)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $repoRoot '.venv\Scripts\python.exe'
$studioExe = 'C:\Program Files\TikTok LIVE Studio\1.36.6\TikTok LIVE Studio.exe'
$adminUrl = 'http://127.0.0.1:8000'
$launchLock = $null
$startedBackend = $false
$backendOwner = $null
$ownedBackendProcesses = @()
$rootBackendIdentity = $null
. (Join-Path $PSScriptRoot 'acceptance-launch-helpers.ps1')
$resultDirectory = Join-Path $repoRoot 'work/acceptance/startup'
New-Item -ItemType Directory -Path $resultDirectory -Force | Out-Null

if (-not $PreparationLaunchId) {
    $launchId = [guid]::NewGuid().ToString('N')
    $latestPath = Join-Path $resultDirectory 'latest.json'
    $result = [pscustomobject][ordered]@{
        launch_id = $launchId; started_at = [DateTime]::UtcNow.ToString('o'); finished_at = $null
        git_head = $null; git_branch = $null; repo_root = $repoRoot
        studio_pid = $null; studio_start_time = $null; studio_integrity = $null
        backend_pid = $null; backend_start_time = $null; backend_integrity = $null; backend_executable = $null
        backend_launch_id = $null; previous_ready_launch_id = $null
        backend_command = $null; backend_parent_pid = $null; startup_configuration_revision = $null
        reused_backend = $false; reused_from_launch_id = $null
        owned_backend_processes = @(); preparation_process = $null
        lmstudio_status = 'not_checked'; model_status = 'not_checked'; backend_health = 'not_checked'; admin_health = 'not_checked'
        account = 'd3pp1s'; numeric_uid_present = $false; auto_off = $false
        one_shot_available = $false; physical_gate = $false; result = 'PENDING'; failure_reason = $null
    }
    try {
        $launchLock = [IO.File]::Open((Join-Path $resultDirectory 'launcher.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
        # Both artifacts exist BEFORE invoking normal UAC. A cancelled attempt
        # always supersedes the previous READY, including a closed console.
        $result = Begin-LaunchResult $resultDirectory $result
        $result.git_head = (& git -C $repoRoot rev-parse HEAD).Trim()
        if ($LASTEXITCODE -ne 0) { throw 'git_unavailable' }
        $result.git_branch = (& git -C $repoRoot branch --show-current).Trim()
        $result = Publish-LaunchResult $resultDirectory $result
        if (-not (Test-AcceptanceBranch $result.git_branch)) { throw 'branch_not_allowed' }
        if (@(& git -C $repoRoot status --porcelain).Count) { throw 'working_tree_dirty' }
        $result = Publish-LaunchResult $resultDirectory $result
        $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
        $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $PSCommandPath + '"'), '-PreparationLaunchId', $launchId)
        if ($NoBrowser) { $arguments += '-NoBrowser' }
        $options = @{ FilePath = (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe')
            ArgumentList = $arguments; WindowStyle = 'Hidden'; PassThru = $true }
        if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            Write-Host 'Approve normal UAC to prepare the backend. No LIVE, ARM or physical input is started.'
            $options.Verb = 'RunAs'
        }
        # Deliberately no Start-Process -Wait (Windows includes descendants).
        $preparation = Start-Process @options
        $terminal = Wait-PreparationResult $resultDirectory $launchId $preparation $PreparationTimeoutSeconds
        Write-Host "$($terminal.result) | $($terminal.failure_reason) | $latestPath"
        if ($terminal.result -ne 'READY') { exit 1 }
        exit 0
    } catch {
        $errorCode = $_.Exception.Message
        $cancelled = $false
        $cause = $_.Exception
        while ($cause) {
            if ($cause -is [ComponentModel.Win32Exception] -and $cause.NativeErrorCode -eq 1223) { $cancelled = $true }
            $cause = $cause.InnerException
        }
        if (Test-Path -LiteralPath (Join-Path $resultDirectory ($launchId + '.json'))) {
            $result = Read-LaunchResult $resultDirectory $launchId
        }
        $result.result = if ($cancelled) { 'CANCELLED' } else { 'FAILED' }
        $allowedReasons = @('git_unavailable', 'branch_not_allowed', 'working_tree_dirty', 'preparation_timeout', 'preparation_exited')
        $result.failure_reason = if ($cancelled) { 'uac_cancelled' } elseif ($errorCode -in $allowedReasons) { $errorCode } else { 'preparation_failed' }
        $result.finished_at = [DateTime]::UtcNow.ToString('o')
        $terminal = Publish-LaunchResult $resultDirectory $result
        # The child checks terminal cancellation before starting a backend and
        # before READY, then cleans up its own identities. Do not terminate it
        # abruptly and orphan a backend; the parent never waits for descendants.
        Write-Host "$($terminal.result) | $($terminal.failure_reason) | $latestPath"
        if ($terminal.result -eq 'READY') { exit 0 }
        exit 1
    } finally { if ($launchLock) { $launchLock.Dispose() } }
}
if ($PreparationLaunchId -cnotmatch '^[a-f0-9]{32}$') { throw 'invalid_launch_id' }
$result = Read-LaunchResult $resultDirectory $PreparationLaunchId
if ($result.result -ne 'PENDING' -or $result.repo_root -ne $repoRoot) { throw 'invalid_launch_attempt' }
$result.preparation_process = Get-ProcessIdentity $PID
$result = Publish-LaunchResult $resultDirectory $result
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
    # Validate both command lines and the exact venv ancestor before reuse.
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

function Assert-LaunchPending {
    if ((Read-LaunchResult $resultDirectory $PreparationLaunchId).result -ne 'PENDING' -or
        (Get-Content -LiteralPath (Join-Path $resultDirectory 'latest.json') -Raw | ConvertFrom-Json).launch_id -cne $PreparationLaunchId) { throw 'attempt_cancelled' }
}

try {
    if (-not (Test-Path -LiteralPath $pythonExe) -or -not (Test-Path -LiteralPath $studioExe)) {
        throw 'The repository venv or expected Studio executable is missing.'
    }
    New-Item -ItemType Directory -Path 'work/pc/logs', 'work/pc/data' -Force | Out-Null
    $runId = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $runDir = Join-Path $repoRoot "work/pc/logs/launch-$runId"
    New-Item -ItemType Directory -Path $runDir | Out-Null
    $receiptPath = Join-Path $repoRoot 'work/pc/acceptance-launch.json'
    $branch = & git branch --show-current
    if ($LASTEXITCODE -ne 0 -or -not (Test-AcceptanceBranch $branch)) {
        throw 'branch_not_allowed'
    }
    $head = & git rev-parse HEAD
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read repository HEAD.' }
    $gitStatus = @(& git status --porcelain)
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read git status.' }
    if ($gitStatus.Count) { throw 'working_tree_dirty' }
    if ($head -cne $result.git_head -or $branch -cne $result.git_branch) { throw 'revision_changed' }
    Write-Host "Repository: $repoRoot | $branch | $head"

    $pocketBefore = ConvertTo-Json -InputObject @(Get-Listeners @(9000, 9002, 9003)) -Compress
    $listeners = @(Get-Listeners @(8000))
    if (-not $listeners.Count -and $result.previous_ready_launch_id) {
        Remove-ReusableCandidate $resultDirectory $result.previous_ready_launch_id
        $result.previous_ready_launch_id = $null
    }
    if ($listeners.Count) {
        if ($listeners.Count -ne 1 -or $listeners[0].LocalAddress -ne '127.0.0.1') {
            throw 'port_in_use'
        }
        $backendOwner = [int]$listeners[0].OwningProcess
        try { Assert-BackendOwner $backendOwner } catch { throw 'port_in_use' }
        Assert-Idle (Get-Admin '/api/admin/status') (Get-Admin '/api/admin/delivery/status')
    }

    $studioGui = @(Get-Process -Name 'TikTok LIVE Studio' -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowHandle -ne 0 })
    if ($studioGui.Count -ne 1) { throw 'Open exactly one Studio GUI manually; no LIVE is started by this script.' }
    $studioId = [int]$studioGui[0].Id
    $result.studio_pid = $studioId
    $result.studio_start_time = $studioGui[0].StartTime.ToUniversalTime().ToFileTimeUtc().ToString()
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
        ACCEPTANCE_LAUNCH_ID = $PreparationLaunchId
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
from app.windows.preparation import StartupPreparation
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
own = own.strip().removeprefix('@').casefold()
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
    startup_configuration_revision=StartupPreparation(s).configuration_revision,
    studio_integrity_rid=studio_integrity, launcher_integrity_rid=launcher_integrity,
    backend_integrity_rid=backend_integrity, own_account=own,
    windows=s.windows.model_dump(mode='json'), journal=counts)))
'@
    $preflightJson = @($preflightCode | & $pythonExe -)
    if ($LASTEXITCODE -ne 0) { throw 'Read-only acceptance preflight failed.' }
    $preflight = ($preflightJson -join "`n") | ConvertFrom-Json
    $result.startup_configuration_revision = $preflight.startup_configuration_revision
    $result.studio_integrity = $preflight.studio_integrity_rid
    $result.numeric_uid_present = [bool]$preflight.windows.broadcaster_user_id
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
    $result.lmstudio_status = 'ready'
    $result.model_status = 'ready'

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
        if ($result.previous_ready_launch_id) {
            $previous = Read-LaunchResult $resultDirectory $result.previous_ready_launch_id
            $reusePreflight = @($preflightCode | & $pythonExe - $backendOwner)
            if ($LASTEXITCODE -ne 0) { throw 'Backend reuse preflight failed.' }
            $reuseIdentity = Get-ProcessIdentity $backendOwner
            $reuseChecks = ($reusePreflight -join "`n") | ConvertFrom-Json
            $binding = [pscustomobject]@{ repo_root=$repoRoot; git_head=$head; git_branch=$branch
                studio_pid=$studioId; studio_start_time=$result.studio_start_time
                studio_integrity=$reuseChecks.studio_integrity_rid; backend_integrity=$reuseChecks.backend_integrity_rid
                startup_configuration_revision=$result.startup_configuration_revision
                preparation=(Get-Admin '/api/admin/status').preparation }
            $reuse = Test-ReusableBackend $previous $reuseIdentity $binding
            if ($reuse) {
                $result.backend_launch_id = $previous.backend_launch_id
                $result.reused_backend = $true; $result.reused_from_launch_id = $previous.launch_id
            }
        }
        # Recheck after inference; an operator may have connected in the meantime.
        Assert-Idle (Get-Admin '/api/admin/status') (Get-Admin '/api/admin/delivery/status')
        if (-not $reuse) {
            throw 'port_in_use'
        }
    }
    Assert-LaunchPending
    if (-not $reuse) {
        $backendProcess = Start-Process -FilePath $pythonExe -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--app-dir', 'backend', '--host', '127.0.0.1', '--port', '8000') -WorkingDirectory $repoRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runDir 'backend.stdout.log') -RedirectStandardError (Join-Path $runDir 'backend.stderr.log')
        $startedBackend = $true
        $rootBackendIdentity = Get-ProcessIdentity $backendProcess.Id
        $ownedBackendProcesses = @(Get-OwnedBackendProcesses $rootBackendIdentity $pythonExe)
        $result.owned_backend_processes = $ownedBackendProcesses
        $result.backend_launch_id = $PreparationLaunchId
        $published = Publish-LaunchResult $resultDirectory $result
        if ($published.result -ne 'PENDING') { throw 'attempt_cancelled' }
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
    if ($startedBackend) {
        $ownedBackendProcesses = @(Get-OwnedBackendProcesses $rootBackendIdentity $pythonExe)
        if ($backendOwner -notin @($ownedBackendProcesses.pid)) { throw 'port_in_use' }
        $result.owned_backend_processes = $ownedBackendProcesses
    }
    Assert-BackendOwner $backendOwner
    $status = Get-Admin '/api/admin/status'
    if ($status.preparation.startup_configuration_revision -cne $result.startup_configuration_revision -or
        $status.preparation.backend_launch_id -cne $result.backend_launch_id) { throw 'Backend startup contract mismatch.' }
    $health = Get-Admin '/health'
    if ($health.status -ne 'ok' -or $health.listener_running) { throw 'backend_health_failed' }
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
    Assert-LaunchPending
    if ((& git rev-parse HEAD) -cne $result.git_head -or @(& git status --porcelain).Count) { throw 'revision_changed' }
    $backendIdentity = Get-ProcessIdentity $backendOwner
    if ($reuse -and -not (Test-ProcessIdentity $reuseIdentity $backendIdentity)) { throw 'port_in_use' }
    $result.backend_pid = $backendOwner
    $result.backend_start_time = $backendIdentity.start_time
    $result.backend_executable = $backendIdentity.executable
    $result.backend_command = $backendIdentity.command
    $result.backend_parent_pid = $backendIdentity.parent_pid
    $result.backend_integrity = $preflight.backend_integrity_rid
    $result.backend_health = 'ready'; $result.admin_health = 'ready'
    $result.auto_off = -not $delivery.windows_sender.auto_enabled
    $result.one_shot_available = $delivery.windows_sender.acceptance_status -eq 'AVAILABLE'
    $result.physical_gate = [bool]$delivery.physical_delivery_enabled
    $receipt = [ordered]@{ prepared_utc = [DateTime]::UtcNow.ToString('o'); repo_root = $repoRoot; head = $head
        backend_pid = $backendOwner; backend_started_utc = (Get-Process -Id $backendOwner).StartTime.ToUniversalTime().ToString('o')
        studio_pid = $studioId; studio_started_utc = $studioGui[0].StartTime.ToUniversalTime().ToString('o')
        preflight = $preflight; application_smoke = $smoke[0].suggestion.status
        admin_ai = $ai; delivery = $delivery; pocketwin_listeners = @(Get-Listeners @(9000, 9002, 9003)) }
    $receipt | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $runDir 'preflight.json') -Encoding UTF8
    Write-AtomicJson $receiptPath $receipt
    $result.result = 'READY'; $result.finished_at = [DateTime]::UtcNow.ToString('o')
    $terminal = Publish-LaunchResult $resultDirectory $result
    if ($terminal.result -ne 'READY') { throw 'attempt_cancelled' }
    Write-Host "PRE-LIVE PREPARED | backend PID $backendOwner | Studio PID $studioId | @d3pp1s"
    Write-Host 'Application smoke READY | AUTO OFF | one-shot AVAILABLE | physical Enter gate ON'
    Write-Host 'Windows focus watch NOT READY: only the operator starts ARM during acceptance.'
    Write-Host "Studio/backend integrity verified: $($preflight.studio_integrity_rid)/$($preflight.backend_integrity_rid)"
    Write-Host "Admin: $adminUrl/ui/ | Evidence: $runDir"
    if (-not $NoBrowser) {
        try { Start-Process "$adminUrl/ui/" } catch { Write-Host "Open Admin manually: $adminUrl/ui/" }
    }
} catch {
    $reason = $_.Exception.Message
    if ($reason -eq 'port_in_use' -and $result.previous_ready_launch_id) {
        Remove-ReusableCandidate $resultDirectory $result.previous_ready_launch_id
    }
    # Never stop a reused server or any unrelated process on a failed check.
    if ($startedBackend) {
        try {
            if ($rootBackendIdentity) { $ownedBackendProcesses = @($ownedBackendProcesses) + @(Get-OwnedBackendProcesses $rootBackendIdentity $pythonExe) }
        } catch { }
        Stop-OwnedBackendProcesses $ownedBackendProcesses
    }
    $result.result = 'FAILED'; $result.finished_at = [DateTime]::UtcNow.ToString('o')
    $result.failure_reason = if ($reason -in @('port_in_use', 'attempt_cancelled', 'revision_changed', 'working_tree_dirty', 'branch_not_allowed')) { $reason } else { 'preparation_check_failed' }
    $terminal = Publish-LaunchResult $resultDirectory $result
    $terminal.failure_reason | Set-Content -LiteralPath (Join-Path $repoRoot 'work/pc/logs/last-launch-error.txt') -Encoding UTF8
    Write-Host "$($terminal.result) | $($terminal.failure_reason) | $(Join-Path $resultDirectory 'latest.json')"
    exit 1
} finally {
    if ($launchLock) { $launchLock.Dispose() }
    Pop-Location
}
