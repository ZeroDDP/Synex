# Сборщик Synex для Windows x64. Воркер: готовый synex_worker.exe из репозитория
# (backend\app\core\target\release) или собранный из core\ (-FromCore, нужен Rust).
#
#   pwsh packaging\windows\build.ps1 [-FromCore] [-NoInstaller] [параметры build.py]
#
# Результат в packaging\dist: Synex-<версия>-windows-x64.zip (папка с Synex.exe) и, если
# установлен Inno Setup 6 (iscc), установщик Synex-<версия>-windows-x64-setup.exe.
param(
    [switch]$FromCore,
    [switch]$NoInstaller,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$BuildArgs = @()
)
$ErrorActionPreference = 'Stop'
$Root = Resolve-Path (Join-Path $PSScriptRoot '..\..')

if ($FromCore) {
    Write-Host '==> Worker: cargo build --release in core\'
    Push-Location (Join-Path $Root 'core')
    try {
        cargo build --release -p synex_worker
        if ($LASTEXITCODE -ne 0) { throw 'cargo build failed' }
    } finally { Pop-Location }
    $Worker = Join-Path $Root 'core\target\release\synex_worker.exe'
} else {
    $Worker = Join-Path $Root 'backend\app\core\target\release\synex_worker.exe'
}

Push-Location $Root
try {
    uv run --project backend python packaging\build.py --worker $Worker @BuildArgs
    if ($LASTEXITCODE -ne 0) { throw 'build.py failed' }

    if (-not $NoInstaller) {
        $Iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
        if (-not $Iscc) {
            $Default = 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
            if (Test-Path $Default) { $Iscc = $Default }
        }
        if ($Iscc) {
            $Version = (Get-Content (Join-Path $Root 'VERSION') -Raw).Trim()
            Write-Host "==> Installer: Inno Setup ($Version)"
            & $Iscc "/DAppVersion=$Version" (Join-Path $Root 'packaging\windows\synex.iss')
            if ($LASTEXITCODE -ne 0) { throw 'Inno Setup failed' }
            $Setup = Join-Path $Root "packaging\dist\Synex-$Version-windows-x64-setup.exe"
            $Hash = (Get-FileHash $Setup -Algorithm SHA256).Hash.ToLower()
            Add-Content (Join-Path $Root 'packaging\dist\SHA256SUMS.txt') "$Hash  $(Split-Path $Setup -Leaf)"
        } else {
            Write-Host '==> Inno Setup not found: only the zip (install it for a setup.exe)'
        }
    }
} finally { Pop-Location }
