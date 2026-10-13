# Read-only preparation against an already running loopback backend.
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
    $options = @{ Uri = "$adminUrl$Path"; TimeoutSec = 45; Method = 'GET' }
    if ($Post) {
        $options.Method = 'POST'
        $options.Headers = @{ 'X-Admin-CSRF' = $script:mobileCsrf; Origin = $adminUrl }
        $options.ContentType = 'application/json'
        $options.Body = '{}'
    }
    Invoke-RestMethod @options
}

function Assert-MobileIdle($Status, $Delivery) {
    if ($Status.active_transport -cne 'android') { throw 'Apply Mobile / Android before preparation.' }
    if ($Status.live.connection_owned -or $Status.live.state -notin @('disconnected','stopped')) { throw 'Disconnect listener first.' }
    if ($Status.automatic_enabled -or $Delivery.mode -cne 'MANUAL') { throw 'AUTO must be OFF.' }
    if ($Delivery.busy -or $Delivery.paused) { throw 'Delivery busy/paused: operator required; never retry UNKNOWN.' }
    if ([bool]$Delivery.physical_delivery_enabled -ne ($ExpectedPhysicalGate -ceq 'ON')) { throw 'Startup physical gate differs from explicit expected value.' }
}

$script:mobileCsrf = (Invoke-MobileRequest '/api/admin/session').csrf_token
$operator = Invoke-MobileRequest '/api/admin/operator/settings'
if ($operator.pending -or $operator.config_error -or $operator.active_transport -cne 'android') { throw 'Save and Apply a valid Mobile profile first.' }
if ($operator.username.ToLowerInvariant() -cne $Username.TrimStart('@').ToLowerInvariant()) { throw 'Applied own username differs from expected account.' }
$status = Invoke-MobileRequest '/api/admin/status'
$delivery = Invoke-MobileRequest '/api/admin/delivery/status'
Assert-MobileIdle $status $delivery
$delivery = Invoke-MobileRequest '/api/admin/device/inspect' -Post
$device = $delivery.inspection
$devices = @($device.devices)
if ($device.adb -cne 'AVAILABLE' -or $devices.Count -ne 1 -or $devices[0].state -cne 'CONNECTED') { throw 'Exactly one expected online Android device is required.' }
if ($DeviceSerial -and $devices[0].serial -cne $DeviceSerial) { throw 'Detected device differs from expected serial.' }
if ($device.serial -cne $devices[0].serial -or $device.security_state) { throw 'Selected device mismatch or security/login dialog: STOP.' }
Write-Host 'Checking own Profile. Open your own TikTok Profile before running this script.'
$verified = Invoke-MobileRequest '/api/admin/device/verify-account' -Post
if (-not $verified.inspection.account_verified -or -not $verified.inspection.profile_verified -or $verified.inspection.security_state) { throw 'Own account/reviewed installation not verified: NEED OPERATOR.' }
# Recheck revisions after read-only awaits; preparation grants no delivery authority.
$finalOperator = Invoke-MobileRequest '/api/admin/operator/settings'
if ($finalOperator.active_runtime_revision -cne $operator.active_runtime_revision -or $finalOperator.pending) { throw 'Runtime changed during preparation. Prepare again.' }
Assert-MobileIdle (Invoke-MobileRequest '/api/admin/status') (Invoke-MobileRequest '/api/admin/delivery/status')
Write-Host 'PREPARE MOBILE complete. AUTO OFF. No input or delivery performed.'
Write-Host 'START LIVE ON PHONE manually -> CONNECT LISTENER -> NEW TEST COMMENT -> AI REPLY -> APPROVE.'
Write-Host 'Inspect host LIVE, open/focus composer manually -> FILL PHONE -> tap Send yourself -> VERIFY -> STOP.'
