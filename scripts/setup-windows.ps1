[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $ProjectRoot

$PythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $PythonCommand) { throw "需要 Python 3.11 或更高版本。" }
& $PythonCommand.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 2)"
if ($LASTEXITCODE -ne 0) { throw "Python 版本低于 3.11。" }

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    & $PythonCommand.Source -m venv (Join-Path $ProjectRoot ".venv")
    if ($LASTEXITCODE -ne 0) { throw "创建本地 Python 环境失败。" }
}

$Node = Get-Command node -ErrorAction SilentlyContinue
if (-not $Node) { throw "需要 Node.js 24.19.0 或更高的 24.x 版本；请安装后重试。" }
$NodeVersionText = ((& $Node.Source -p "process.versions.node").Trim())
$NodeVersion = [version]$NodeVersionText
if ($NodeVersion -lt [version]"24.19.0" -or $NodeVersion -ge [version]"25.0.0") {
    throw "当前 Node.js 版本为 $NodeVersionText，需要 >=24.19.0 且 <25.0.0。"
}

$Pnpm = Get-Command pnpm -ErrorAction SilentlyContinue
if (-not $Pnpm) { throw "需要 pnpm；请先启用 Corepack 或安装 pnpm。" }

if (Test-Path -LiteralPath (Join-Path $ProjectRoot "pnpm-lock.yaml") -PathType Leaf) {
    & $Pnpm.Source install --frozen-lockfile --ignore-scripts
} else {
    & $Pnpm.Source install --ignore-scripts
}
if ($LASTEXITCODE -ne 0) { throw "安装 Node.js 依赖失败。" }

& $VenvPython -m company_platform validate
if ($LASTEXITCODE -ne 0) { throw "插件与工作流校验失败。" }
& $VenvPython -m company_platform self-test
if ($LASTEXITCODE -ne 0) { throw "默认业务域受控流程自检失败。" }
& $Pnpm.Source test:runtime
if ($LASTEXITCODE -ne 0) { throw "Pi 扩展、Skill 或 TypeScript 运行时验证失败。" }

Write-Output ([pscustomobject]@{
    status = "ok"
    project = $ProjectRoot
    python = (& $VenvPython --version)
    node = (& $Node.Source --version)
    next = ".\scripts\start-windows.ps1"
} | ConvertTo-Json -Compress)
