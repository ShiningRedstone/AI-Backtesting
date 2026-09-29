# Build the EdgeLab desktop app for Windows (developers only).
#   powershell -ExecutionPolicy Bypass -File build_windows.ps1 [-SkipFrontend] [-Smoke]
# Prerequisites: Python 3.11+ (64-bit), Node 18+ with npm (the build runs `npm ci` in web\ to install
# the locked frontend dependencies from web\package-lock.json, network needed; -SkipFrontend uses the
# committed bundle instead), Git (for the recorded commit). End users need none of these.
# Output: dist\EdgeLab\EdgeLab.exe (opens its own EdgeLab window; needs the Microsoft Edge WebView2 Runtime,
#         built into Windows 11 / installed on Windows 10 by Windows Update) + EdgeLabConsole.exe (console: cli, logs).
#         Copy the whole dist\EdgeLab folder; user data is NOT in it.
# -Smoke: headless packaged smoke test, then the native-window test (a window opens and closes by itself).
param([switch]$SkipFrontend, [switch]$Smoke)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venv = Join-Path $PSScriptRoot ".venv-build"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    Write-Host "==> creating build environment $venv"
    python -m venv $venv
}
$py = Join-Path $venv "Scripts\python.exe"
& $py -m pip install --upgrade pip
& $py -m pip install -r packaging\requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw "dependency installation failed" }

$argsList = @("packaging\build.py")
if ($SkipFrontend) { $argsList += "--skip-frontend" }
if ($Smoke) { $argsList += "--smoke" }
& $py @argsList
if ($LASTEXITCODE -ne 0) { throw "build failed (exit $LASTEXITCODE)" }
Write-Host "`nDone: dist\EdgeLab\EdgeLab.exe"
