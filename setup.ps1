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

Write-Host "Creating Start Menu shortcut..." -ForegroundColor Cyan
$sh  = New-Object -ComObject WScript.Shell
$dir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$oldLnk = Join-Path $dir "Claude Usage Widget.lnk"
if (Test-Path $oldLnk) { Remove-Item $oldLnk -Force }
$lnk = $sh.CreateShortcut((Join-Path $dir "AI Usage Widget.lnk"))
$lnk.TargetPath       = "wscript.exe"
$lnk.Arguments        = "`"$vbs`""
$lnk.WorkingDirectory = $repo
if (Test-Path $icon) { $lnk.IconLocation = $icon }
$lnk.Description      = "Floating AI usage widget"
$lnk.Save()

# Note: "Launch on startup" is opt-in via the widget's settings panel.

Write-Host "Launching the widget..." -ForegroundColor Cyan
Start-Process wscript -ArgumentList "`"$vbs`""

Write-Host ""
Write-Host "Done. Look for the widget in the bottom-right of your screen." -ForegroundColor Green
Write-Host "It will auto-start every time you log in."
