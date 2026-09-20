[CmdletBinding()]
param([switch]$Apply, [switch]$ResumeIncomplete)
$ErrorActionPreference = 'Stop'
$Source = Split-Path -Parent $MyInvocation.MyCommand.Path
$Destination = 'E:\Minecarft\.E3_LLBot_Sanae\liandong-monitor'
$TaskName = 'Sanae Liandong Shop Monitor'
$Files = @('config.json', 'monitor_core.py', 'deepseek_classifier.py', 'shop_source.py', 'shop_monitor.py', 'priority_alert.py', 'start-monitor.ps1', 'stop-monitor.ps1', 'README.md')
foreach ($Name in $Files) {
    if (-not (Test-Path -LiteralPath (Join-Path $Source $Name))) { throw "Missing source: $Name" }
}
if (Test-Path -LiteralPath $Destination) {
    if (-not $ResumeIncomplete) { throw 'Destination exists; review it before an update' }
    if (Test-Path -LiteralPath (Join-Path $Destination 'state')) { throw 'Runtime state exists; not an incomplete fresh install' }
    foreach ($Name in $Files) {
        if ((Get-FileHash -LiteralPath (Join-Path $Destination $Name)).Hash -ne (Get-FileHash -LiteralPath (Join-Path $Source $Name)).Hash) { throw "Existing deployment changed: $Name" }
    }
}
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) { throw 'Monitor task already exists' }
$Existing = Get-ScheduledTask -TaskName 'Sanae LLBot Bridge'
$Identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Plan = [ordered]@{source=$Source; destination=$Destination; task=$TaskName; user=$Identity; group=1092470719; files=$Files; mode='plan'}
if (-not $Apply) { $Plan | ConvertTo-Json -Depth 3; exit 0 }
$null = New-Item -ItemType Directory -Path $Destination -Force
$Hashes = @()
foreach ($Name in $Files) {
    $From = Join-Path $Source $Name
    $To = Join-Path $Destination $Name
    Copy-Item -LiteralPath $From -Destination $To
    $Hash = (Get-FileHash -LiteralPath $From -Algorithm SHA256).Hash
    if ((Get-FileHash -LiteralPath $To -Algorithm SHA256).Hash -ne $Hash) { throw "Copy hash mismatch: $Name" }
    $Hashes += [pscustomobject]@{name=$Name; sha256=$Hash}
}
$Action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + (Join-Path $Destination 'start-monitor.ps1') + '"') -WorkingDirectory $Destination
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $Identity
$Principal = New-ScheduledTaskPrincipal -UserId $Identity -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$null = Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description 'Watch four approved public WZYP shops every 20s; Sanae OneBot -> QQ 1092470719; DeepSeek title classification.' -ErrorAction Stop
$null = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
if (Test-Path -LiteralPath (Join-Path $Destination 'installation.json')) {
    Copy-Item -LiteralPath (Join-Path $Destination 'installation.json') -Destination (Join-Path $Destination ('installation-incomplete-' + (Get-Date -Format yyyyMMdd-HHmmss) + '.json'))
}
$Receipt = [ordered]@{installed_at=(Get-Date -Format o);destination=$Destination;task=$TaskName;files=$Hashes;group=1092470719;autostart='user logon';existing_bridge_changed=$false}
$Receipt | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $Destination 'installation.json') -Encoding UTF8
Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
$Receipt | ConvertTo-Json -Depth 4
