"""Stock PowerShell offline harness. No UAC, actual backend, Studio or input."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.windows, pytest.mark.live_studio]
SCRIPTS = Path('scripts').resolve()


def run_ps(tmp_path, code, *, expected=0):
    shell = shutil.which('powershell.exe')
    if not shell: pytest.skip('Windows PowerShell harness')
    harness = tmp_path/'harness.ps1'
    harness.write_text("$ErrorActionPreference = 'Stop'\nSet-StrictMode -Version Latest\n" + code, encoding='utf-8-sig')
    result = subprocess.run([shell, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(harness)],
        capture_output=True, text=True, timeout=15)
    assert result.returncode == expected, result.stdout + result.stderr
    return result


def copy_launcher(tmp_path):
    scripts = tmp_path/'scripts'; scripts.mkdir()
    for name in ['start-acceptance.ps1', 'acceptance-launch-helpers.ps1']:
        shutil.copy2(SCRIPTS/name, scripts/name)
    return scripts/'start-acceptance.ps1'


def test_branch_policy_atomic_terminal_result_and_bounded_wait(tmp_path):
    code = f". '{SCRIPTS / 'acceptance-launch-helpers.ps1'}'\n$directory = '{tmp_path}'\n" + r'''
if (-not (Test-AcceptanceBranch 'feature/windows-live-studio') -or -not (Test-AcceptanceBranch 'main')) { throw 'allowed branches rejected' }
foreach ($branch in @('other', '', 'MAIN', 'feature/unreviewed')) {
    if (Test-AcceptanceBranch $branch) { throw 'unreviewed branch accepted' }
}
$value = [pscustomobject]@{launch_id='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';result='PENDING';failure_reason=$null}
$null = Publish-LaunchResult $directory $value
$process = [pscustomobject]@{HasExited=$false}
try { $null = Wait-PreparationResult $directory $value.launch_id $process 0; throw 'timeout missed' }
catch { if ($_.Exception.Message -ne 'preparation_timeout') { throw } }
$value.result = 'FAILED'; $value.failure_reason = 'preparation_timeout'
$null = Publish-LaunchResult $directory $value
$value.result = 'READY'
$terminal = Publish-LaunchResult $directory $value
if ($terminal.result -ne 'FAILED') { throw 'late child revived failure' }
$latest = Get-Content -LiteralPath (Join-Path $directory 'latest.json') -Raw | ConvertFrom-Json
if ($latest.result -ne 'FAILED') { throw 'latest revived failure' }
if (@(Get-ChildItem -LiteralPath $directory -Filter '*.tmp').Count) { throw 'non atomic remnants' }
'''
    run_ps(tmp_path, code)


def test_uac_cancel_supersedes_previous_ready_before_start_process(tmp_path):
    launcher = copy_launcher(tmp_path)
    directory = tmp_path/'work/acceptance/startup'; directory.mkdir(parents=True)
    old = {'launch_id':'b'*32, 'result':'READY'}
    (directory/'latest.json').write_text(json.dumps(old), encoding='utf-8')
    code = r'''
function git {
    $global:LASTEXITCODE = 0
    if ($args -contains 'rev-parse') { 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' }
    elseif ($args -contains 'branch') { 'feature/windows-live-studio' }
}
function Start-Process {
    $latest = Get-Content -LiteralPath '__DIRECTORY__/latest.json' -Raw | ConvertFrom-Json
    if ($latest.result -ne 'PENDING' -or $latest.launch_id -eq 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb') { throw 'pending missing before UAC' }
    throw (New-Object ComponentModel.Win32Exception(1223))
}
& '__LAUNCHER__' -NoBrowser
exit $LASTEXITCODE
'''.replace('__DIRECTORY__', str(directory)).replace('__LAUNCHER__', str(launcher))
    run_ps(tmp_path, code, expected=1)
    latest = json.loads((directory/'latest.json').read_text(encoding='utf-8-sig'))
    assert latest['result'] == 'CANCELLED' and latest['failure_reason'] == 'uac_cancelled'
    assert latest['launch_id'] != old['launch_id'] and latest['finished_at']
    assert latest == json.loads((directory/(latest['launch_id']+'.json')).read_text(encoding='utf-8-sig'))


@pytest.mark.parametrize('changed', ['none', 'start_time', 'executable', 'command', 'parent_pid'])
def test_cleanup_only_stops_captured_exact_process_identity(tmp_path, changed):
    code = f". '{SCRIPTS / 'acceptance-launch-helpers.ps1'}'\n" + r'''
$expected = [pscustomobject]@{pid=42;start_time='100';executable='C:/repo/.venv/Scripts/python.exe';command='python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000';parent_pid=10}
$actual = $expected.PSObject.Copy()
__CHANGE__
$script:stopped = @()
function Get-ProcessIdentity([int]$ProcessId) { if ($ProcessId -ne 42) { throw 'foreign pid queried' }; return $actual }
function Stop-Process([int]$Id) { $script:stopped += $Id }
Stop-OwnedBackendProcesses @($expected)
if ($script:stopped.Count -ne __COUNT__) { throw 'ownership violation' }
'''
    change = '' if changed == 'none' else f"$actual.{changed} = " + ('999' if changed == 'parent_pid' else "'different'")
    run_ps(tmp_path, code.replace('__CHANGE__', change).replace('__COUNT__', '1' if changed == 'none' else '0'))


def test_foreign_occupied_port_is_failed_without_stop(tmp_path):
    launcher = copy_launcher(tmp_path)
    venv = tmp_path/'.venv/Scripts'; venv.mkdir(parents=True); (venv/'python.exe').touch()
    directory = tmp_path/'work/acceptance/startup'; directory.mkdir(parents=True)
    launch_id = 'a'*32
    value = {'launch_id':launch_id, 'result':'PENDING', 'repo_root':str(tmp_path),
        'previous_ready_launch_id':None,
        'git_head':'b'*40, 'git_branch':'main', 'preparation_process':None, 'failure_reason':None, 'finished_at':None}
    (directory/(launch_id+'.json')).write_text(json.dumps(value), encoding='utf-8')
    (directory/'latest.json').write_text(json.dumps(value), encoding='utf-8')
    code = r'''
function git {
    $global:LASTEXITCODE = 0
    if ($args -contains 'rev-parse') { 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' }
    elseif ($args -contains 'branch') { 'main' }
}
function Test-Path {
    param([string]$LiteralPath)
    if ($LiteralPath -like '*Program Files*TikTok LIVE Studio*') { return $true }
    Microsoft.PowerShell.Management\Test-Path -LiteralPath $LiteralPath
}
function Get-NetTCPConnection { [pscustomobject]@{LocalAddress='127.0.0.1';LocalPort=8000;OwningProcess=54321} }
function Get-CimInstance { [pscustomobject]@{ProcessId=54321;CreationDate=[DateTime]::UtcNow;ExecutablePath='C:/foreign/python.exe';CommandLine='unrelated';ParentProcessId=1} }
function Stop-Process { throw 'FOREIGN PROCESS STOPPED' }
& '__LAUNCHER__' -NoBrowser -PreparationLaunchId aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
exit $LASTEXITCODE
'''.replace('__LAUNCHER__', str(launcher))
    # The preparation-process identity query must name the harness PID; mock it
    # separately through CIM while the foreign port owner keeps its own image.
    code = code.replace('ProcessId=54321;CreationDate', 'ProcessId=$PID;CreationDate')
    run_ps(tmp_path, code, expected=1)
    latest = json.loads((directory/'latest.json').read_text(encoding='utf-8-sig'))
    assert latest['result'] == 'FAILED' and latest['failure_reason'] == 'port_in_use'


def test_launcher_parent_never_uses_wait_or_port_based_cleanup():
    source = (SCRIPTS/'start-acceptance.ps1').read_text(encoding='utf-8-sig')
    active = '\n'.join(line for line in source.splitlines() if not line.lstrip().startswith('#'))
    assert '-WindowStyle Hidden -Wait' not in active and '-Wait -PassThru' not in active
    assert 'Stop-Process -Id $backendOwner' not in source
    assert 'Stop-Process -Id $listener' not in source
    assert 'Assert-LaunchPending' in source and 'preparation_timeout' in source


@pytest.mark.parametrize('changed', ['none', 'parent', 'command', 'start'])
def test_venv_descendant_ownership_is_rechecked_after_enumeration(tmp_path, changed):
    code = f". '{SCRIPTS / 'acceptance-launch-helpers.ps1'}'\n" + r'''
$command = 'python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000'
$root = [pscustomobject]@{pid=42;start_time='100';executable='C:\repo\.venv\Scripts\python.exe';command=$command;parent_pid=10}
$child = [pscustomobject]@{pid=43;start_time='200';executable='C:\Python\python.exe';command=$command;parent_pid=42}
__CHANGE__
function Get-ProcessIdentity([int]$ProcessId) { if ($ProcessId -eq 42) { return $root }; if ($ProcessId -eq 43) { return $child }; throw 'foreign pid' }
function Get-CimInstance { @([pscustomobject]@{ProcessId=43;CommandLine=$command;CreationDate=[DateTime]::FromFileTimeUtc(200)}, [pscustomobject]@{ProcessId=999;CommandLine='foreign';CreationDate=[DateTime]::FromFileTimeUtc(200)}) }
function Stop-Process { throw 'no process may stop in ownership enumeration' }
$owned = @(Get-OwnedBackendProcesses $root 'C:\repo\.venv\Scripts\python.exe')
if ($owned.Count -ne __COUNT__) { throw 'descendant ownership not rechecked' }
'''
    changes = {'none':'', 'parent':'$child.parent_pid = 999', 'command':"$child.command = 'unrelated'", 'start':"$child.start_time = '50'"}
    run_ps(tmp_path, code.replace('__CHANGE__', changes[changed]).replace('__COUNT__', '2' if changed == 'none' else '1'))


def recovery_fixture(tmp_path):
    return f". '{SCRIPTS / 'acceptance-launch-helpers.ps1'}'\n$directory = '{tmp_path}'\n" + r'''
function New-Attempt([string]$id) {
    [pscustomobject]@{launch_id=$id; result='PENDING'; failure_reason=$null; finished_at=$null; previous_ready_launch_id=$null
        repo_root='C:/repo'; git_head=('d'*40); git_branch='main'; backend_pid=42; backend_start_time='100'
        backend_executable='C:/Python/python.exe'; backend_command='python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000'
        backend_parent_pid=41; backend_launch_id=('a'*32); startup_configuration_revision=('e'*64)
        studio_pid=99; studio_start_time='200'; account='d3pp1s'; numeric_uid_present=$true
        physical_gate=$true; auto_off=$true; one_shot_available=$true; reused_backend=$false; reused_from_launch_id=$null}
}
$a = Begin-LaunchResult $directory (New-Attempt ('a'*32))
$a.result='READY'; $a = Publish-LaunchResult $directory $a
$identity = [pscustomobject]@{pid=42;start_time='100';executable=$a.backend_executable;command=$a.backend_command;parent_pid=41}
$binding = [pscustomobject]@{repo_root='C:/repo';git_head=('d'*40);git_branch='main';studio_pid=99;studio_start_time='200'
    studio_integrity=12288;backend_integrity=12288;startup_configuration_revision=('e'*64)
    preparation=[pscustomobject]@{backend_launch_id=('a'*32);startup_configuration_revision=('e'*64)}}
'''


def test_a_ready_b_cancelled_c_revalidates_and_publishes_own_ready(tmp_path):
    run_ps(tmp_path, recovery_fixture(tmp_path) + r'''
$b = Begin-LaunchResult $directory (New-Attempt ('b'*32))
if ($b.previous_ready_launch_id -cne $a.launch_id) { throw 'candidate lost at B' }
$b.result='CANCELLED'; $b.failure_reason='uac_cancelled'; $null = Publish-LaunchResult $directory $b
if ((Read-ReusableCandidate $directory).launch_id -cne $a.launch_id) { throw 'cancel removed candidate' }
$c = Begin-LaunchResult $directory (New-Attempt ('c'*32))
if ($c.result -ne 'PENDING' -or $c.previous_ready_launch_id -cne $a.launch_id) { throw 'C did not start fresh' }
$candidate = Read-LaunchResult $directory $c.previous_ready_launch_id
if (-not (Test-ReusableBackend $candidate $identity $binding)) { throw 'valid runtime rejected' }
$c.backend_launch_id=$candidate.backend_launch_id; $c.reused_backend=$true; $c.reused_from_launch_id=$candidate.launch_id
$c.result='READY'; $null=Publish-LaunchResult $directory $c
$latest=Get-Content (Join-Path $directory 'latest.json') -Raw | ConvertFrom-Json
if ($latest.launch_id -cne $c.launch_id -or -not $latest.reused_backend -or $latest.reused_from_launch_id -cne $a.launch_id) { throw 'old READY reused as current result' }
if ((Read-LaunchResult $directory $b.launch_id).result -ne 'CANCELLED') { throw 'B revived' }
if ((Read-ReusableCandidate $directory).launch_id -cne $c.launch_id) { throw 'candidate not advanced atomically' }
if (@(Get-ChildItem $directory -Filter '*.tmp').Count) { throw 'atomic remnants' }
''')


@pytest.mark.parametrize('invalid', ['dead', 'pid_reuse', 'head', 'unknown_owner', 'executable', 'command',
    'parent', 'studio_restart', 'integrity', 'configuration', 'backend_configuration', 'launch_nonce', 'physical', 'uid', 'account'])
def test_reuse_candidate_requires_current_runtime_and_contract(tmp_path, invalid):
    changes = {
        'dead':'$identity=$null', 'pid_reuse':"$identity.start_time='999'", 'head':"$binding.git_head=('f'*40)",
        'unknown_owner':'$identity.pid=900', 'executable':"$identity.executable='C:/foreign/python.exe'",
        'command':"$identity.command='unrelated'", 'parent':'$identity.parent_pid=999',
        'studio_restart':"$binding.studio_start_time='999'", 'integrity':'$binding.backend_integrity=8192',
        'configuration':"$binding.startup_configuration_revision=('f'*64)",
        'backend_configuration':"$binding.preparation.startup_configuration_revision=('f'*64)",
        'launch_nonce':"$binding.preparation.backend_launch_id=('f'*32)",
        'physical':'$a.physical_gate=$false', 'uid':'$a.numeric_uid_present=$false', 'account':"$a.account='other'"}
    run_ps(tmp_path, recovery_fixture(tmp_path) + r'''
$b = Begin-LaunchResult $directory (New-Attempt ('b'*32))
$b.result='CANCELLED'; $null=Publish-LaunchResult $directory $b
$c = Begin-LaunchResult $directory (New-Attempt ('c'*32))
if ($c.previous_ready_launch_id -cne $a.launch_id) { throw 'candidate lost after cancellation' }
''' + changes[invalid] + r'''
function Stop-Process { throw 'unknown process must never stop' }
if (Test-ReusableBackend $a $identity $binding) { throw 'stale/foreign candidate accepted' }
Remove-ReusableCandidate $directory $a.launch_id
if ($null -ne (Read-ReusableCandidate $directory)) { throw 'invalid candidate retained' }
''')


@pytest.mark.parametrize('terminal', ['CANCELLED', 'PENDING'])
def test_late_child_cannot_overwrite_current_generation_or_last_ready(tmp_path, terminal):
    run_ps(tmp_path, recovery_fixture(tmp_path) + r'''
$b = Begin-LaunchResult $directory (New-Attempt ('b'*32))
__TERMINAL__
$c = Begin-LaunchResult $directory (New-Attempt ('c'*32))
$b.result='READY'; $stale=Publish-LaunchResult $directory $b
if ($stale.result -ne 'CANCELLED') { throw 'stale terminal revived' }
$latest=Get-Content (Join-Path $directory 'latest.json') -Raw | ConvertFrom-Json
if ($latest.launch_id -cne $c.launch_id -or $latest.result -ne 'PENDING') { throw 'stale child replaced current generation' }
if ((Read-ReusableCandidate $directory).launch_id -cne $a.launch_id) { throw 'stale child poisoned reusable candidate' }
$c.result='READY'; $null=Publish-LaunchResult $directory $c
Remove-ReusableCandidate $directory $a.launch_id
if ((Read-ReusableCandidate $directory).launch_id -cne $c.launch_id) { throw 'late invalidation removed newer candidate' }
'''.replace('__TERMINAL__', "$b.result='CANCELLED'; $null=Publish-LaunchResult $directory $b" if terminal=='CANCELLED' else ''))


def test_parent_cancel_preserves_last_ready_for_next_attempt(tmp_path):
    launcher=copy_launcher(tmp_path)
    directory=tmp_path/'work/acceptance/startup'; directory.mkdir(parents=True)
    ready={'launch_id':'a'*32,'result':'READY'}
    for name in ['latest.json', 'last-ready.json', 'a'*32+'.json']:
        (directory/name).write_text(json.dumps(ready),encoding='utf-8')
    code=r'''
function git { $global:LASTEXITCODE=0; if ($args -contains 'rev-parse') { 'dddddddddddddddddddddddddddddddddddddddd' } elseif ($args -contains 'branch') { 'main' } }
function Start-Process {
    $latest=Get-Content '__DIRECTORY__/latest.json' -Raw | ConvertFrom-Json
    if ($latest.result -ne 'PENDING' -or $latest.previous_ready_launch_id -cne ('a'*32)) { throw 'candidate link lost before UAC' }
    throw (New-Object ComponentModel.Win32Exception(1223))
}
& '__LAUNCHER__' -NoBrowser
exit $LASTEXITCODE
'''.replace('__DIRECTORY__',str(directory)).replace('__LAUNCHER__',str(launcher))
    for _ in range(2):
        run_ps(tmp_path,code,expected=1)
        latest=json.loads((directory/'latest.json').read_text(encoding='utf-8-sig'))
        assert latest['result']=='CANCELLED' and latest['previous_ready_launch_id']=='a'*32
        assert json.loads((directory/'last-ready.json').read_text())==ready


def test_concurrent_generations_only_publish_current_ready(tmp_path):
    run_ps(tmp_path, recovery_fixture(tmp_path) + r'''
$helpers='__HELPERS__'
$jobs=@()
try {
    foreach ($id in @(('b'*32),('c'*32))) {
        $jobs += Start-Job -ArgumentList $directory,$helpers,(New-Attempt $id) -ScriptBlock {
            param($directory,$helpers,$value)
            $ErrorActionPreference='Stop'; . $helpers
            $value=Begin-LaunchResult $directory $value
            [IO.File]::WriteAllText((Join-Path $directory ($value.launch_id+'.started')), 'started')
            $timer=[Diagnostics.Stopwatch]::StartNew()
            while (-not (Test-Path (Join-Path $directory 'release'))) {
                if ($timer.Elapsed.TotalSeconds -gt 8) { throw 'barrier timeout' }; Start-Sleep -Milliseconds 20
            }
            $value.result='READY'; $null=Publish-LaunchResult $directory $value
        }
    }
    $timer=[Diagnostics.Stopwatch]::StartNew()
    while (@(Get-ChildItem $directory -Filter '*.started').Count -lt 2) {
        if ($timer.Elapsed.TotalSeconds -gt 8) { throw 'start timeout' }; Start-Sleep -Milliseconds 20
    }
    [IO.File]::WriteAllText((Join-Path $directory 'release'), 'release')
    $null=Wait-Job $jobs -Timeout 8
    if (@($jobs | Where-Object State -ne 'Completed').Count) { throw 'publication job failed' }
    $jobs | Receive-Job -ErrorAction Stop | Out-Null
    $latest=Get-Content (Join-Path $directory 'latest.json') -Raw | ConvertFrom-Json
    if ($latest.result -ne 'READY' -or (Read-ReusableCandidate $directory).launch_id -cne $latest.launch_id) { throw 'current/candidate mismatch' }
    $values=@((Read-LaunchResult $directory ('b'*32)),(Read-LaunchResult $directory ('c'*32)))
    if (@($values | Where-Object result -eq 'READY').Count -ne 1 -or @($values | Where-Object result -eq 'CANCELLED').Count -ne 1) { throw 'stale generation published READY' }
} finally { if ($jobs.Count) { $jobs | Stop-Job; $jobs | Remove-Job } }
'''.replace('__HELPERS__',str(SCRIPTS/'acceptance-launch-helpers.ps1')))
