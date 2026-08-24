[CmdletBinding()]
param(
    [switch]$WorkbenchOnly,
    [switch]$SelfTest,
    [string]$Profile = $env:AGENT4COMPANY_PROFILE
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $ProjectRoot

if (-not $Profile) { $Profile = "company-manager" }
if ($Profile -notin @("company-manager", "company-with-sales")) {
    throw "未知工作台组合：$Profile"
}
$env:AGENT4COMPANY_PROFILE = $Profile

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $PythonCommand) { throw "未找到 Python 3.11+，请先运行 scripts/setup-windows.ps1。" }
    $Python = $PythonCommand.Source
}

if ($SelfTest) {
    & $Python -m company_platform validate
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $Python -m company_platform self-test
    exit $LASTEXITCODE
}

if ($WorkbenchOnly) {
    & $Python -m company_platform serve --profile $Profile --port 8766 --open-browser
    exit $LASTEXITCODE
}

$PackagedApp = Join-Path $ProjectRoot "Agent4Company.exe"
if (Test-Path -LiteralPath $PackagedApp -PathType Leaf) {
    & $PackagedApp
    exit $LASTEXITCODE
}

$Pnpm = Get-Command pnpm -ErrorAction SilentlyContinue
if (-not $Pnpm) { throw "未找到 pnpm；请先运行 scripts/setup-windows.ps1。" }
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot "node_modules\.bin\tauri.CMD") -PathType Leaf)) {
    throw "项目依赖尚未安装；请先运行 scripts/setup-windows.ps1。"
}
& $Pnpm.Source desktop:dev
exit $LASTEXITCODE
