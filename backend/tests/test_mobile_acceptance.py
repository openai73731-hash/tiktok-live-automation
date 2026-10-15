"""PowerShell Mobile preparation with fake HTTP responses; zero devices/process input."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("failure", ["none", "transport", "own_account", "pending", "listener", "auto", "gate",
    "multiple", "offline", "wrong_device", "security", "unprepared", "false_verified", "revision", "recover",
    "still_paused", "final_paused", "recovery_refused", "busy", "hierarchy_unavailable"])
def test_mobile_preparation_has_no_input_and_fails_closed(failure):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell: pytest.skip("PowerShell unavailable")
    script = r'''
$ErrorActionPreference='Stop'
$global:mobileFixtureFailure=__FAILURE__
$global:mobileFixtureReads=0
$global:mobileFixturePaths=@()
$global:mobileFixturePrepared=$false
function Invoke-RestMethod {
 param($Uri,$TimeoutSec,$Method,$Headers,$ContentType,$Body)
 $path=([uri]$Uri).AbsolutePath
 $global:mobileFixturePaths += $path
 if ($Method -eq 'POST' -and $path -cne '/api/admin/delivery/prepare') { throw 'Physical, listener or Profile proof action attempted' }
 if ($Method -eq 'POST' -and ($Body -cne '{}' -or -not $Headers['X-Admin-CSRF'])) { throw 'Unexpected browser authority' }
 switch ($path) {
  '/api/admin/session' { return @{csrf_token=('c'*32)} }
  '/api/admin/operator/settings' {
   $global:mobileFixtureReads++
   return @{pending=($global:mobileFixtureFailure -eq 'pending');config_error=$null;active_transport= $(if($global:mobileFixtureFailure -eq 'transport'){'windows_live_studio'}else{'android'});
    username=$(if($global:mobileFixtureFailure -eq 'own_account'){'foreign'}else{'own'});active_runtime_revision=$(if($global:mobileFixturePrepared){'c'*32}elseif($global:mobileFixtureFailure -eq 'revision' -and $global:mobileFixtureReads -gt 1){'b'*32}else{'a'*32})}
  }
  '/api/admin/delivery/prepare' {
   if($global:mobileFixtureFailure -eq 'recovery_refused'){throw 'operator_paused / recovery required'}
   $global:mobileFixturePrepared=$true
   $devices=@(@{serial='fixture';state=$(if($global:mobileFixtureFailure -eq 'offline'){'OFFLINE'}else{'CONNECTED'})})
   if($global:mobileFixtureFailure -eq 'multiple'){$devices+=@{serial='second';state='CONNECTED'}}
   return @{paused=($global:mobileFixtureFailure -eq 'still_paused');active_runtime_revision=('c'*32);
    mobile_prepared=($global:mobileFixtureFailure -ne 'unprepared');inspection=@{adb='AVAILABLE';devices=$devices;
     serial=$(if($global:mobileFixtureFailure -eq 'wrong_device'){'foreign'}else{'fixture'});
     security_state=($global:mobileFixtureFailure -eq 'security');account_verified=($global:mobileFixtureFailure -eq 'false_verified');profile_verified=$false}}
  }
  '/api/admin/status' { return @{active_transport='android';automatic_enabled=($global:mobileFixtureFailure -eq 'auto');live=@{connection_owned=($global:mobileFixtureFailure -eq 'listener');state='disconnected'}} }
  default {
   $devices=@(@{serial='fixture';state=$(if($global:mobileFixtureFailure -eq 'offline'){'OFFLINE'}else{'CONNECTED'})})
   if($global:mobileFixtureFailure -eq 'multiple'){$devices+=@{serial='second';state='CONNECTED'}}
   return @{mode='MANUAL';busy=($global:mobileFixtureFailure -eq 'busy');paused=(($global:mobileFixtureFailure -eq 'recover' -and -not $global:mobileFixturePrepared) -or ($global:mobileFixtureFailure -eq 'final_paused' -and $global:mobileFixturePrepared));physical_delivery_enabled=($global:mobileFixtureFailure -eq 'gate');
    inspection=@{adb='AVAILABLE';reason=$(if($global:mobileFixtureFailure -eq 'hierarchy_unavailable'){'hierarchy_unavailable'}else{'not_checked'});devices=$devices;serial=$(if($global:mobileFixtureFailure -eq 'wrong_device'){'foreign'}else{'fixture'});
     security_state=($global:mobileFixtureFailure -eq 'security');account_verified=$false;profile_verified=$false}}
  }
 }
}
try {
 & './scripts/prepare-mobile.ps1' -Username own -DeviceSerial fixture
 if($global:mobileFixtureFailure -notin @('none','recover','hierarchy_unavailable')){throw 'Unsafe preflight accepted'}
 if($global:mobileFixturePaths -contains '/api/admin/device/verify-account'){throw 'Preparation must not require or claim account verification'}
 if($global:mobileFixturePaths -notcontains '/api/admin/delivery/prepare'){throw 'Missing explicit recovery'}
} catch {
 if($global:mobileFixtureFailure -in @('none','recover','hierarchy_unavailable') -or $_.Exception.Message -eq 'Unsafe preflight accepted'){throw}
}
exit 0
'''.replace("__FAILURE__", "'" + failure + "'")
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr


def test_mobile_preparation_documents_manual_start_and_has_no_physical_route():
    script = Path("scripts/prepare-mobile.ps1").read_text(encoding="utf-8")
    assert "START LIVE ON PHONE manually" in script
    assert "/api/admin/live/connect" not in script and "/api/admin/delivery/fill" not in script
    assert "adb.exe" not in script and "SendInput" not in script
