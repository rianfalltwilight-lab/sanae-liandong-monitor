param(
    [ValidateSet('priority','quiet','shops','analytics')][string]$UpdateName = 'priority',
    [string[]]$Changes = @('shop_monitor.py','priority_alert.py','README.md','stop-monitor.ps1')
)
$ErrorActionPreference = 'Stop'
$Source = Split-Path -Parent $MyInvocation.MyCommand.Path
$Destination = 'E:\Minecarft\.E3_LLBot_Sanae\liandong-monitor'
$TaskName = 'Sanae Liandong Shop Monitor'
$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
if ($Task.Actions[0].Arguments -notlike ('*' + $Destination + '\start-monitor.ps1*')) { throw 'Unexpected monitor task identity' }
$ManifestPath = Join-Path $Destination 'installation.json'
$Manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($Entry in $Manifest.files) {
    if ((Get-FileHash -LiteralPath (Join-Path $Destination $Entry.name) -Algorithm SHA256).Hash -ne $Entry.sha256) { throw ('Unexpected deployed edit: ' + $Entry.name) }
}
foreach ($Name in $Changes) {
    if (-not (Test-Path -LiteralPath (Join-Path $Source $Name))) { throw ('Missing source: ' + $Name) }
}
$Backup = Join-Path $Destination ('backups\' + $UpdateName + '-' + (Get-Date -Format yyyyMMdd-HHmmss))
$null = New-Item -ItemType Directory -Path $Backup
$BackupNames = @($Changes + @('installation.json') | Select-Object -Unique | Where-Object {Test-Path -LiteralPath (Join-Path $Destination $_)})
foreach ($Name in $BackupNames) {
    Copy-Item -LiteralPath (Join-Path $Destination $Name) -Destination (Join-Path $Backup $Name)
}
$ProcessRecord = Get-Content -LiteralPath (Join-Path $Destination 'state\process.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop
for ($Attempt=0; $Attempt -lt 10; $Attempt++) {
    $Worker = Get-CimInstance Win32_Process -Filter ('ProcessId=' + [int]$ProcessRecord.pid)
    if (-not $Worker) { break }
    Start-Sleep -Milliseconds 500
}
if ($Worker) {
    if ($Worker.Name -ne 'python.exe' -or -not $Worker.CommandLine.Contains($Destination + '\shop_monitor.py')) { throw 'Unexpected worker identity; no process stopped' }
    $Launcher = Get-CimInstance Win32_Process -Filter ('ProcessId=' + [int]$Worker.ParentProcessId)
    Stop-Process -Id ([int]$Worker.ProcessId) -Force -ErrorAction Stop
    if ($Launcher -and $Launcher.Name -eq 'python.exe' -and $Launcher.CommandLine.Contains($Destination + '\shop_monitor.py')) {
        Stop-Process -Id ([int]$Launcher.ProcessId) -Force -ErrorAction SilentlyContinue
    }
    if (Get-CimInstance Win32_Process -Filter ('ProcessId=' + [int]$ProcessRecord.pid)) { throw 'Monitor worker still running' }
}
Copy-Item -LiteralPath (Join-Path $Destination 'state\monitor.json') -Destination (Join-Path $Backup 'monitor-state.json')
try {
    foreach ($Name in $Changes) {
        Copy-Item -LiteralPath (Join-Path $Source $Name) -Destination (Join-Path $Destination $Name)
        if ((Get-FileHash -LiteralPath (Join-Path $Source $Name)).Hash -ne (Get-FileHash -LiteralPath (Join-Path $Destination $Name)).Hash) { throw 'Copy verification failed' }
    }
    $Names = @($Manifest.files.name) + $Changes | Select-Object -Unique
    $Manifest.files = @($Names | ForEach-Object { [pscustomobject]@{name=$_;sha256=(Get-FileHash -LiteralPath (Join-Path $Destination $_) -Algorithm SHA256).Hash} })
    $Manifest | Add-Member -NotePropertyName ($UpdateName + '_update_at') -NotePropertyValue (Get-Date -Format o) -Force
    $Manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $ManifestPath -Encoding UTF8
} catch {
    foreach ($Name in $BackupNames) {
        Copy-Item -LiteralPath (Join-Path $Backup $Name) -Destination (Join-Path $Destination $Name)
    }
    $null = Enable-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    throw
}
$null = Enable-ScheduledTask -TaskName $TaskName -ErrorAction Stop
Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
[pscustomobject]@{task=$TaskName;backup=$Backup;changed=$Changes;group=1092470719;mention=@('3294692833','1920924896');threshold='price < 40; stock > 0; 2FA supplied';statePreserved=$true} | ConvertTo-Json -Depth 4
