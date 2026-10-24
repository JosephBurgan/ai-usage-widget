# AI Usage Widget — one-command setup.
# Usage:  Right-click → "Run with PowerShell"
#   or:   powershell -ExecutionPolicy Bypass -File setup.ps1

$ErrorActionPreference = "Stop"
$repo  = $PSScriptRoot
$icon  = Join-Path $repo "claude_widget.ico"
$vbs   = Join-Path $repo "claude_widget.vbs"

function Need-Command($name, $hint) {
  if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
    Write-Host "Missing: $name. $hint" -ForegroundColor Red
    exit 1
  }
}

Need-Command python "Install Python 3.10+ from python.org (check 'Add to PATH')."
Need-Command git    "Install Git from git-scm.com or GitHub Desktop."

Write-Host "Installing Python dependencies..." -ForegroundColor Cyan
python -m pip install --quiet --user -r (Join-Path $repo "requirements.txt")

# --from-startup must match STARTUP_ARG in claude_widget.py.
function Set-WidgetShortcut([string]$Path, [switch]$FromStartup) {
  $sh = New-Object -ComObject WScript.Shell
  $lnk = $sh.CreateShortcut($Path)
  $lnk.TargetPath = "wscript.exe"
  if ($FromStartup) {
    $lnk.Arguments = "`"$vbs`" --from-startup"
  } else {
    $lnk.Arguments = "`"$vbs`""
  }
  $lnk.WorkingDirectory = $repo
  if (Test-Path $icon) { $lnk.IconLocation = $icon }
  $lnk.Description = "Floating AI usage widget"
  $lnk.Save()
}

Write-Host "Creating Start Menu shortcut..." -ForegroundColor Cyan
$menuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$oldMenu = Join-Path $menuDir "Claude Usage Widget.lnk"
if (Test-Path $oldMenu) { Remove-Item $oldMenu -Force }
Set-WidgetShortcut (Join-Path $menuDir "AI Usage Widget.lnk")

$startupDir = [Environment]::GetFolderPath("Startup")
$startupLnk = Join-Path $startupDir "AI Usage Widget.lnk"
$oldStartup = Join-Path $startupDir "Claude Usage Widget.lnk"
if ((Test-Path $oldStartup) -and -not (Test-Path $startupLnk)) {
  Rename-Item -Path $oldStartup -NewName "AI Usage Widget.lnk"
}

$settingsPath = Join-Path $env:USERPROFILE ".claude_widget_settings.json"
$seedPy = @'
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from claude_widget import seed_startup_settings_file
on, tray = seed_startup_settings_file(Path(sys.argv[2]), Path(sys.argv[3]))
print("on" if on else "off")
print("tray" if tray else "window")
'@
$seeded = @(python -c $seedPy $repo $settingsPath $startupLnk)
if ($LASTEXITCODE -ne 0) { throw "Could not read startup settings." }
$launchOn = $seeded[0] -eq "on"
$trayOnly = $seeded[1] -eq "tray"

if ($launchOn) {
  Write-Host "Creating Startup shortcut..." -ForegroundColor Cyan
  Set-WidgetShortcut $startupLnk -FromStartup
} elseif (Test-Path $startupLnk) {
  Remove-Item $startupLnk -Force
  Write-Host "Removed the Startup shortcut to match Launch on startup." -ForegroundColor Cyan
}

Write-Host "Launching the widget..." -ForegroundColor Cyan
Start-Process wscript -ArgumentList "`"$vbs`""

Write-Host ""
Write-Host "Done. Look for the widget in the bottom-right of your screen." -ForegroundColor Green
if ($launchOn -and $trayOnly) {
  Write-Host "The next sign-in starts it as a taskbar icon. Click that icon to open the window."
} elseif ($launchOn) {
  Write-Host "It will open every time you sign in."
} else {
  Write-Host "Launch on startup is off. Turn it on from the widget settings."
}
