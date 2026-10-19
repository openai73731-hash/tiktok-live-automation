# Explicit safe MANUAL preparation against an already running loopback backend.
# Never starts LIVE/listener, selects AUTO, fills, taps or sends.
[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidatePattern('^@?[A-Za-z0-9._]{1,24}$')][string]$Username,
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$')][string]$DeviceSerial,
    [ValidateRange(1024,65535)][int]$Port = 8000,
    [ValidateSet('OFF','ON')][string]$ExpectedPhysicalGate = 'OFF'
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$adminUrl = "http://127.0.0.1:$Port"

function Invoke-MobileRequest([string]$Path, [switch]$Post) {
    $options = @{ Uri = "$adminUrl$Path"; TimeoutSec = 180; Method = 'GET' }
    if ($Post) {
        $options.Method = 'POST'
        $options.Headers = @{ 'X-Admin-CSRF' = $script:mobileCsrf; Origin = $adminUrl }
        $options.ContentType = 'application/json'
        $options.Body = '{}'
    }
    Invoke-RestMethod @options
}

function Assert-MobileIdle($Status, $Delivery, [switch]$AllowRecovery) {
    if ($Status.active_transport -cne 'android') { throw 'Apply Mobile / Android before preparation.' }
    if ($Status.live.connection_owned -or $Status.live.state -notin @('disconnected','stopped')) { throw 'operator_paused / recovery required: disconnect listener first.' }
    if ($Status.automatic_enabled -or $Delivery.mode -cne 'MANUAL') { throw 'AUTO must be OFF.' }
    if ($Delivery.busy) { throw 'Delivery busy: recovery required; wait for cleanup.' }
    if ($Delivery.paused -and -not $AllowRecovery) { throw 'operator_paused / recovery required; never retry UNKNOWN.' }
    if ([bool]$Delivery.physical_delivery_enabled -ne ($ExpectedPhysicalGate -ceq 'ON')) { throw 'Startup physical gate differs from explicit expected value.' }
}

$script:mobileCsrf = (Invoke-MobileRequest '/api/admin/session').csrf_token
$operator = Invoke-MobileRequest '/api/admin/operator/settings'
if ($operator.pending -or $operator.config_error -or $operator.active_transport -cne 'android') { throw 'Save and Apply a valid Mobile profile first.' }
if ($operator.username.ToLowerInvariant() -cne $Username.TrimStart('@').ToLowerInvariant()) { throw 'Applied own username differs from expected account.' }
$status = Invoke-MobileRequest '/api/admin/status'
$delivery = Invoke-MobileRequest '/api/admin/delivery/status'
Assert-MobileIdle $status $delivery -AllowRecovery
Write-Host 'Preparing the exact reviewed phone/installation and checking the visible own Profile automatically.'
# Explicit recovery creates a new runtime revision; journals/consumed rows remain intact.
$beforePrepare = Invoke-MobileRequest '/api/admin/operator/settings'
if ($beforePrepare.active_runtime_revision -cne $operator.active_runtime_revision -or $beforePrepare.pending -or
    $beforePrepare.username.ToLowerInvariant() -cne $Username.TrimStart('@').ToLowerInvariant()) { throw 'Runtime/account changed before preparation. Prepare again.' }
$prepared = Invoke-MobileRequest '/api/admin/delivery/prepare' -Post
if ($prepared.paused) { throw 'operator_paused / recovery required: preparation did not clear the safety pause.' }
$device = $prepared.inspection
$devices = @($device.devices)
if (-not $prepared.mobile_prepared) { throw 'Device/install preparation did not PASS.' }
if ($device.account_verified -and -not $device.profile_verified) { throw 'Account verification requires the reviewed Profile.' }
if ($device.adb -cne 'AVAILABLE' -or $devices.Count -ne 1 -or $devices[0].state -cne 'CONNECTED') { throw 'Exactly one expected online Android device is required.' }
if ($DeviceSerial -and $devices[0].serial -cne $DeviceSerial) { throw 'Detected device differs from expected serial.' }
if ($device.serial -cne $devices[0].serial -or $device.security_state) { throw 'Selected device mismatch or security/login dialog: STOP.' }
$finalOperator = Invoke-MobileRequest '/api/admin/operator/settings'
if ($finalOperator.active_runtime_revision -cne $prepared.active_runtime_revision -or $finalOperator.pending) { throw 'Runtime changed during preparation. Prepare again.' }
Assert-MobileIdle (Invoke-MobileRequest '/api/admin/status') (Invoke-MobileRequest '/api/admin/delivery/status')
Write-Host "PREPARE MOBILE complete. Own Profile verified: $($device.account_verified). AUTO OFF. No input or delivery performed."
if (-not $device.account_verified) { Write-Host 'Open your exact own Profile before LIVE and use Prepare and check in the panel.' }
Write-Host 'START LIVE ON PHONE manually -> CONNECT LISTENER -> automatic account/session checks.'
Write-Host 'Then NEW EXTERNAL TEST COMMENT -> AI REPLY -> APPROVE.'
Write-Host 'Inspect host LIVE, open/focus composer manually -> FILL PHONE -> tap Send yourself -> VERIFY -> STOP.'
