# Create a desktop shortcut that launches Pic2TeX with pythonw (no console).
#
# Usage: powershell -ExecutionPolicy Bypass -File .\packaging\install_shortcut.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Target = Join-Path $Root "Pic2TeX.pyw"
if (-not (Test-Path $Target)) {
    throw "找不到 Pic2TeX.pyw：$Target"
}

$pyw = (Get-Command pyw.exe -ErrorAction SilentlyContinue).Source
if (-not $pyw) {
    $pyw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
}
if (-not $pyw) {
    throw "未找到 pyw.exe / pythonw.exe，请确认已安装 Python。"
}

$Desktop = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $Desktop "Pic2TeX.lnk"
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $pyw
$Shortcut.Arguments = '"' + $Target + '"'
$Shortcut.WorkingDirectory = $Root
$Shortcut.Description = "Pic2TeX · 截图转 LaTeX"
$IconPath = Join-Path $Root "assets\icon.ico"
if (Test-Path $IconPath) { $Shortcut.IconLocation = $IconPath }
$Shortcut.Save()

Write-Host "已创建桌面快捷方式: $ShortcutPath"
