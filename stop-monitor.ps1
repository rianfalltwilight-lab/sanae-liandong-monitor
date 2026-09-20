[CmdletBinding()]
param([switch]$RemoveTask)
$ErrorActionPreference = 'Stop'
$TaskName = 'Sanae Liandong Shop Monitor'
$ExpectedRoot = 'E:\Minecarft\.E3_LLBot_Sanae\liandong-monitor'
$ProcessRecordPath = Join-Path $ExpectedRoot 'state\process.json'
$ProcessRecord = if (Test-Path -LiteralPath $ProcessRecordPath) { Get-Content -LiteralPath $ProcessRecordPath -Raw -Encoding UTF8 | ConvertFrom-Json } else { $null }
$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Task) {
    if ($Task.Actions.Arguments -notlike ('*' + $ExpectedRoot + '\start-monitor.ps1*')) { throw 'Unexpected task action' }
    Disable-ScheduledTask -TaskName $TaskName | Out-Null
    Stop-ScheduledTask -TaskName $TaskName
    if ($RemoveTask) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false }
}
if ($ProcessRecord) {
    $Worker = Get-CimInstance Win32_Process -Filter ('ProcessId=' + [int]$ProcessRecord.pid)
    if ($Worker) {
        if ($Worker.Name -ne 'python.exe' -or -not $Worker.CommandLine.Contains($ExpectedRoot + '\shop_monitor.py')) { throw 'Unexpected worker identity; no process stopped' }
        $Launcher = Get-CimInstance Win32_Process -Filter ('ProcessId=' + [int]$Worker.ParentProcessId)
        Stop-Process -Id ([int]$Worker.ProcessId) -Force -ErrorAction Stop
        if ($Launcher -and $Launcher.Name -eq 'python.exe' -and $Launcher.CommandLine.Contains($ExpectedRoot + '\shop_monitor.py')) {
            Stop-Process -Id ([int]$Launcher.ProcessId) -Force -ErrorAction SilentlyContinue
        }
    }
}
# Retain catalog state, receipts and logs for a reversible pause/rollback.
Write-Output 'Monitor disabled and stopped; source/state retained. Sanae bridge is unchanged.'
