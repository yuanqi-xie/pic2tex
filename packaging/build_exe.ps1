# Build a standalone Pic2TeX.exe with PyInstaller (one file, windowed).
#
# Usage (from the repository root or anywhere):
#   powershell -ExecutionPolicy Bypass -File .\packaging\build_exe.ps1
#   powershell -ExecutionPolicy Bypass -File .\packaging\build_exe.ps1 -NoInstall
#
# The script picks Python 3.11+ (PyInstaller support), installs PyInstaller if
# needed, bundles assets\ and writes dist\Pic2TeX.exe.

[CmdletBinding()]
param(
    [switch]$NoInstall
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Resolve-PythonSpec {
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
        throw "未找到 Python 启动器 py。请安装 Python 3.11+ 并勾选 py launcher。"
    }
    foreach ($spec in @("-3.11", "-3.12", "-3.13", "-3.14", "-3")) {
        & py $spec --version *> $null
        if ($LASTEXITCODE -eq 0) { return $spec }
    }
    throw "未找到可用的 Python 3.11+ 解释器。"
}

$spec = Resolve-PythonSpec
Write-Host "使用解释器: py $spec"

if (-not $NoInstall) {
    Write-Host "安装 / 更新 PyInstaller …"
    & py $spec -m pip install --upgrade pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 安装失败。" }
}

if (-not (Test-Path "assets\icon.ico")) {
    throw "缺少 assets\icon.ico，请先运行: py $spec tools\make_icon.py"
}

Write-Host "开始打包 …"
& py $spec -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name Pic2TeX `
    --icon "assets\icon.ico" `
    --add-data "assets;assets" `
    "Pic2TeX.pyw"

if ($LASTEXITCODE -ne 0 -or -not (Test-Path "dist\Pic2TeX.exe")) {
    throw "打包失败，请查看上面的 PyInstaller 输出。"
}

$sizeMb = (Get-Item "dist\Pic2TeX.exe").Length / 1MB
Write-Host ("构建完成: dist\Pic2TeX.exe ({0:N1} MB)" -f $sizeMb)
Write-Host "可直接复制该 exe 到任意 Windows 机器运行（仍需填入自己的 DeepSeek API Key）。"
