# Run the kwatchdog daemon at logon, hidden, restarting on failure.
#   powershell -ExecutionPolicy Bypass -File deploy\install-windows-task.ps1
#   (remove with: Unregister-ScheduledTask -TaskName kwatchdog -Confirm:$false)
param(
    [string]$Python = (Get-Command pythonw.exe -ErrorAction Stop).Source,
    [string]$TaskName = "kwatchdog"
)
$action = New-ScheduledTaskAction -Execute $Python -Argument "-m kwatchdog daemon --quiet"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "kwatchdog monitoring daemon" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "kwatchdog daemon registered and started ($Python -m kwatchdog daemon --quiet)"
