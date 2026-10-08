# Read-only worker. No InvokePattern, SetValue, SetFocus, keyboard, clipboard or mouse APIs.
param(
    [int]$TargetProcessId = 0,
    [ValidateRange(1, 2000)][int]$MaxNodes = 500,
    [ValidateRange(1, 20)][int]$MaxDepth = 12,
    [switch]$IncludeNames,
    [ValidateSet('control', 'raw')][string]$TreeView = 'control'
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$script:nodes = New-Object System.Collections.Generic.List[object]
$script:visited = 0
$script:unreadable = 0
$script:truncated = $false

function Short-Text([string]$Value) {
    if ($null -eq $Value) { return '' }
    $Value = $Value -replace '[\x00-\x1f\x7f]', ' '
    return $Value.Substring(0, [Math]::Min(160, $Value.Length))
}

function Walk-Node($Element, $ParentId, [int]$Depth, [int]$WindowId) {
    if ($script:visited -ge $MaxNodes) { $script:truncated = $true; return }
    $script:visited++
    try {
        $current = $Element.Current
        $protected = $current.IsPassword
        $nodeId = $script:visited
        # Never read ValuePattern/TextPattern or password names/content.
        $name = $null
        if ($IncludeNames -and -not $protected) { $name = Short-Text $current.Name }
        $kind = $current.ControlType.ProgrammaticName -replace '^ControlType\.', ''
        $rect = $current.BoundingRectangle
        $bounds = $null
        if (-not $rect.IsEmpty) { $bounds = @([int]$rect.X, [int]$rect.Y, [int]$rect.Width, [int]$rect.Height) }
        $patterns = @()
        if (-not $protected) {
            $patterns = @($Element.GetSupportedPatterns() | Select-Object -First 16 | ForEach-Object { Short-Text $_.ProgrammaticName })
        }
        $script:nodes.Add([ordered]@{
            node_id = $nodeId; parent_id = $ParentId; window_id = $WindowId; depth = $Depth
            process_id = $current.ProcessId; native_handle = $current.NativeWindowHandle
            control_type = $kind; automation_id = Short-Text $current.AutomationId
            name = $name; class_name = Short-Text $current.ClassName
            enabled = $current.IsEnabled; focusable = $current.IsKeyboardFocusable
            focused = $current.HasKeyboardFocus; offscreen = $current.IsOffscreen
            password = $protected; bounds = $bounds; supported_patterns = $patterns
            candidate_edit = (-not $protected -and $kind -eq 'Edit')
            candidate_button = (-not $protected -and $kind -eq 'Button')
            candidate_chat_region = (-not $protected -and ($kind -in @('List', 'Document') -or ($name -and $name -match '(?i)chat|comment|reply|чат|комментар|回复')))
        })
        if ($protected) { return } # Skip the entire protected subtree.
        $child = $script:walker.GetFirstChild($Element)
        if ($null -ne $child -and $Depth -ge $MaxDepth) { $script:truncated = $true; return }
        while ($null -ne $child) {
            if ($script:visited -ge $MaxNodes) { $script:truncated = $true; break }
            Walk-Node $child $nodeId ($Depth + 1) $WindowId
            $child = $script:walker.GetNextSibling($child)
        }
    } catch {
        $script:unreadable++ # Never serialize exception text or UI fragments.
    }
}

try {
    $processes = @(Get-Process | Where-Object {
        if ($TargetProcessId -gt 0) { $_.Id -eq $TargetProcessId }
        else { $_.ProcessName -match '(?i)tiktok.*live.?studio|live.?studio' }
    } | Select-Object -First 64)
    $processInfo = @($processes | ForEach-Object { @{ process_id = $_.Id; name = Short-Text $_.ProcessName } })
    $windowCount = 0
    if ($processes.Count -gt 0) {
        Add-Type -AssemblyName UIAutomationClient | Out-Null
        Add-Type -AssemblyName UIAutomationTypes | Out-Null
        Add-Type -AssemblyName WindowsBase | Out-Null
        $script:walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
        if ($TreeView -eq 'raw') { $script:walker = [System.Windows.Automation.TreeWalker]::RawViewWalker }
        # Enumerate only desktop children; never walk unrelated applications.
        $topWalker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
        $desktop = [System.Windows.Automation.AutomationElement]::RootElement
        $top = $topWalker.GetFirstChild($desktop)
        $topCount = 0
        while ($null -ne $top) {
            $topCount++
            if ($topCount -gt 512) { $script:truncated = $true; break }
            try {
                if ($top.Current.ProcessId -in $processes.Id) {
                    if ($windowCount -ge 8) { $script:truncated = $true; break }
                    $windowCount++
                    Walk-Node $top $null 0 $windowCount
                }
            } catch { $script:unreadable++ }
            $top = $topWalker.GetNextSibling($top)
        }
    }
    $status = 'window_not_found'
    if ($windowCount -gt 0) { $status = 'discovered' }
    if ($script:truncated -or $script:unreadable -gt 0) { $status = 'unknown_ui_state' }
    [ordered]@{
        status = $status; tree_view = $TreeView; names_included = [bool]$IncludeNames
        processes = $processInfo; windows = $windowCount; nodes = @($script:nodes.ToArray())
        truncated = $script:truncated; unreadable = $script:unreadable
    } | ConvertTo-Json -Depth 8 -Compress
} catch {
    '{"status":"uia_unavailable"}'
}
