# Pure orchestration helpers; dot-source in offline tests without UAC or backend.
function Write-AtomicJson([string]$Path, $Value) {
    $temporary = $Path + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    try {
        [IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Depth 12), (New-Object Text.UTF8Encoding($false)))
        if ([IO.File]::Exists($Path)) { [IO.File]::Replace($temporary, $Path, [NullString]::Value) }
        else { [IO.File]::Move($temporary, $Path) }
    } finally { if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) } }
}

function Read-LaunchResult([string]$Directory, [string]$LaunchId) {
    Get-Content -LiteralPath (Join-Path $Directory ($LaunchId + '.json')) -Raw | ConvertFrom-Json
}

function Open-PublicationLock([string]$Directory) {
    $lock = $null
    $timer = [Diagnostics.Stopwatch]::StartNew()
    while (-not $lock) {
        try { $lock = [IO.File]::Open((Join-Path $Directory 'publication.lock'), 'OpenOrCreate', 'ReadWrite', 'None') }
        catch { if ($timer.Elapsed.TotalSeconds -ge 5) { throw 'result_lock_timeout' }; Start-Sleep -Milliseconds 50 }
    }
    return $lock
}

function Read-ReusableCandidate([string]$Directory) {
    $path = Join-Path $Directory 'last-ready.json'
    try {
        $candidate = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
        if ($candidate.result -eq 'READY' -and $candidate.launch_id -cmatch '^[a-f0-9]{32}$') { return $candidate }
    } catch { }
    return $null
}

function Begin-LaunchResult([string]$Directory, $Value) {
    $lock = Open-PublicationLock $Directory
    try {
        $latest = Join-Path $Directory 'latest.json'
        $candidate = Read-ReusableCandidate $Directory
        if (Test-Path -LiteralPath $latest) {
            $old = Get-Content -LiteralPath $latest -Raw | ConvertFrom-Json
            if ($old.result -eq 'PENDING') {
                $old.result = 'CANCELLED'; $old.failure_reason = 'superseded'
                $old.finished_at = [DateTime]::UtcNow.ToString('o')
                Write-AtomicJson (Join-Path $Directory ($old.launch_id + '.json')) $old
            }
        }
        $Value.previous_ready_launch_id = if ($candidate) { $candidate.launch_id } else { $null }
        Write-AtomicJson (Join-Path $Directory ($Value.launch_id + '.json')) $Value
        Write-AtomicJson $latest $Value
        return $Value
    } finally { $lock.Dispose() }
}

function Remove-ReusableCandidate([string]$Directory, [string]$LaunchId) {
    $lock = Open-PublicationLock $Directory
    try {
        $candidate = Read-ReusableCandidate $Directory
        if ($candidate -and $candidate.launch_id -ceq $LaunchId) {
            [IO.File]::Delete((Join-Path $Directory 'last-ready.json'))
        }
    } finally { $lock.Dispose() }
}

function Publish-LaunchResult([string]$Directory, $Value) {
    # One publication lock also orders generation changes and last-ready writes.
    # Terminal results are immutable. Only the current generation may be READY.
    $lock = Open-PublicationLock $Directory
    try {
        $path = Join-Path $Directory ($Value.launch_id + '.json')
        if (Test-Path -LiteralPath $path) {
            $old = Read-LaunchResult $Directory $Value.launch_id
            if ($old.result -ne 'PENDING') { return $old }
        }
        $latest = Join-Path $Directory 'latest.json'
        $current = -not (Test-Path -LiteralPath $latest) -or
            (Get-Content -LiteralPath $latest -Raw | ConvertFrom-Json).launch_id -ceq $Value.launch_id
        if (-not $current -and $Value.result -in @('PENDING', 'READY')) {
            $Value.result = 'CANCELLED'; $Value.failure_reason = 'superseded'
        }
        Write-AtomicJson $path $Value
        if ($current) {
            Write-AtomicJson $latest $Value
            if ($Value.result -eq 'READY') { Write-AtomicJson (Join-Path $Directory 'last-ready.json') $Value }
        }
        return $Value
    } finally { $lock.Dispose() }
}

function Test-ReusableBackend($Candidate, $Identity, $Binding) {
    # Presence of last-ready is never readiness. All fields below are freshly
    # rechecked, including the backend's immutable startup contract/launch nonce.
    try {
        return [bool]($Candidate -and $Identity -and $Candidate.result -ceq 'READY' -and
            $Candidate.repo_root -ieq $Binding.repo_root -and $Candidate.git_head -ceq $Binding.git_head -and
            (Test-AcceptanceBranch $Candidate.git_branch) -and (Test-AcceptanceBranch $Binding.git_branch) -and
            $Candidate.backend_pid -eq $Identity.pid -and $Candidate.backend_start_time -ceq $Identity.start_time -and
            $Candidate.backend_executable -ieq $Identity.executable -and $Candidate.backend_command -ceq $Identity.command -and
            $Candidate.backend_parent_pid -eq $Identity.parent_pid -and
            $Identity.command -match '(?i) -m uvicorn app\.main:app --app-dir backend --host 127\.0\.0\.1 --port 8000\s*$' -and
            $Candidate.studio_pid -eq $Binding.studio_pid -and $Candidate.studio_start_time -ceq $Binding.studio_start_time -and
            $Binding.backend_integrity -ge $Binding.studio_integrity -and
            $Candidate.account -ceq 'd3pp1s' -and $Candidate.numeric_uid_present -eq $true -and
            $Candidate.physical_gate -eq $true -and $Candidate.auto_off -eq $true -and $Candidate.one_shot_available -eq $true -and
            $Candidate.startup_configuration_revision -ceq $Binding.startup_configuration_revision -and
            $Binding.preparation.startup_configuration_revision -ceq $Binding.startup_configuration_revision -and
            $Candidate.backend_launch_id -cmatch '^[a-f0-9]{32}$' -and
            $Binding.preparation.backend_launch_id -ceq $Candidate.backend_launch_id)
    } catch { return $false }
}

function Test-AcceptanceBranch([string]$Branch) {
    $Branch -cin @('feature/windows-live-studio', 'main')
}

function Get-ProcessIdentity([int]$ProcessId) {
    $p = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
    if (-not $p) { return $null }
    [pscustomobject]@{ pid = [int]$p.ProcessId; start_time = (Get-Process -Id $ProcessId).StartTime.ToUniversalTime().ToFileTimeUtc().ToString()
        executable = [string]$p.ExecutablePath; command = [string]$p.CommandLine; parent_pid = [int]$p.ParentProcessId }
}

function Test-ProcessIdentity($Expected, $Actual) {
    $null -ne $Actual -and $Expected.pid -eq $Actual.pid -and
        $Expected.start_time -ceq $Actual.start_time -and $Expected.executable -ieq $Actual.executable -and
        $Expected.command -ceq $Actual.command -and $Expected.parent_pid -eq $Actual.parent_pid
}

function Get-OwnedBackendProcesses($RootIdentity, [string]$PythonExe) {
    $commandPattern = '(?i) -m uvicorn app\.main:app --app-dir backend --host 127\.0\.0\.1 --port 8000\s*$'
    if (-not $RootIdentity -or $RootIdentity.executable -ine $PythonExe -or $RootIdentity.command -notmatch $commandPattern) { return @() }
    $root = Get-ProcessIdentity $RootIdentity.pid
    if (-not (Test-ProcessIdentity $RootIdentity $root)) { return @() }
    $owned = @($root)
    # Windows venv redirector -> base Python only. No generic descendant kill.
    foreach ($p in @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($root.pid)")) {
        if ($p.CommandLine -match $commandPattern -and $p.CreationDate.ToUniversalTime().ToFileTimeUtc() -ge [long]$root.start_time) {
            $identity = Get-ProcessIdentity ([int]$p.ProcessId)
            if ($identity -and $identity.parent_pid -eq $root.pid -and
                [long]$identity.start_time -ge [long]$root.start_time -and $identity.command -match $commandPattern -and
                [IO.Path]::GetFileName($identity.executable) -ieq 'python.exe') { $owned += $identity }
        }
    }
    if (-not (Test-ProcessIdentity $RootIdentity (Get-ProcessIdentity $RootIdentity.pid))) { return @() }
    return $owned
}

function Stop-OwnedBackendProcesses($Identities) {
    # Re-read each creation identity immediately before stopping. PID reuse,
    # different command/image/parent, and unrecorded port owners are untouched.
    foreach ($expected in @($Identities | Sort-Object start_time -Descending)) {
        try {
            if (Test-ProcessIdentity $expected (Get-ProcessIdentity ([int]$expected.pid))) {
                Stop-Process -Id $expected.pid -ErrorAction Stop
            }
        } catch { }
    }
}

function Wait-PreparationResult([string]$Directory, [string]$LaunchId, $Process, [int]$TimeoutSeconds) {
    $timer = [Diagnostics.Stopwatch]::StartNew()
    while ($timer.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        $value = Read-LaunchResult $Directory $LaunchId
        if ($value.result -ne 'PENDING') { return $value }
        # HasExited observes ONLY this preparation process, never descendants.
        if ($Process.HasExited) { throw 'preparation_exited' }
        Start-Sleep -Milliseconds 200
    }
    throw 'preparation_timeout'
}
