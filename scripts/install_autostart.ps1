# Start Spoke at login on Windows (shortcut in the per-user Startup folder).
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Uninstall
param([switch]$Uninstall)
$ErrorActionPreference = "Stop"

$Repo = Split-Path -Parent $PSScriptRoot
$PyW = Join-Path $Repo ".venv\Scripts\pythonw.exe"   # pythonw = no console window
$Startup = [Environment]::GetFolderPath("Startup")
$Lnk = Join-Path $Startup "Spoke.lnk"

if ($Uninstall) {
    if (Test-Path $Lnk) { Remove-Item $Lnk }
    Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" |
        Where-Object { $_.CommandLine -like "*-m spoke*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Write-Host "Removed $Lnk"
    exit 0
}

if (-not (Test-Path $PyW)) {
    Write-Error "No venv at $Repo\.venv -- run the install steps in README.md first."
}

$Shell = New-Object -ComObject WScript.Shell
$S = $Shell.CreateShortcut($Lnk)
$S.TargetPath = $PyW
$S.Arguments = "-m spoke"
$S.WorkingDirectory = $Repo
$S.Description = "Spoke push-to-talk dictation"
$S.Save()

Start-Process -FilePath $PyW -ArgumentList "-m", "spoke" -WorkingDirectory $Repo -WindowStyle Hidden
Write-Host "Installed $Lnk and started Spoke (logs: $env:USERPROFILE\.spoke\spoke.log)."
Write-Host "Note: Spoke runs non-elevated, so it cannot paste into apps running as Administrator."
Write-Host "Uninstall: powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1 -Uninstall"
