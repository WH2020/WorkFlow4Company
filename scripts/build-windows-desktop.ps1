[CmdletBinding()]
param(
    [string]$OutputPath,
    [switch]$SkipSelfTest
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $ProjectRoot
if (-not $OutputPath) { $OutputPath = Join-Path $ProjectRoot "Agent4Company.exe" }
$OutputPath = [IO.Path]::GetFullPath($OutputPath)

$Pnpm = Get-Command pnpm -ErrorAction SilentlyContinue
if (-not $Pnpm) { throw "未找到 pnpm；请先运行 scripts/setup-windows.ps1。" }
$Cargo = Get-Command cargo -ErrorAction SilentlyContinue
if (-not $Cargo) { throw "未找到 Rust/Cargo；请安装 rustup 和 Microsoft C++ Build Tools。" }
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $PythonCommand) { throw "未找到 Python 3.11+；请先运行 scripts/setup-windows.ps1。" }
    $Python = $PythonCommand.Source
}

& $Pnpm.Source check:types
if ($LASTEXITCODE -ne 0) { throw "TypeScript 类型检查失败。" }
& $Pnpm.Source test:runtime
if ($LASTEXITCODE -ne 0) { throw "Pi 扩展、Skill 或 TypeScript 运行时验证失败。" }
& $Python -m company_platform self-test
if ($LASTEXITCODE -ne 0) { throw "公司平台运行时自检失败。" }

$Manifest = Join-Path $ProjectRoot "desktop\src-tauri\Cargo.toml"
& $Cargo.Source build --manifest-path $Manifest --release --locked
if ($LASTEXITCODE -ne 0) { throw "Tauri 桌面编译失败，退出码 $LASTEXITCODE。" }

$BuiltExecutable = Join-Path $ProjectRoot "desktop\src-tauri\target\release\Agent4Company.exe"
if (-not (Test-Path -LiteralPath $BuiltExecutable -PathType Leaf)) {
    throw "未生成桌面可执行文件：$BuiltExecutable"
}
$OutputDirectory = Split-Path -Parent $OutputPath
if (-not (Test-Path -LiteralPath $OutputDirectory -PathType Container)) {
    New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
}
Copy-Item -LiteralPath $BuiltExecutable -Destination $OutputPath -Force

if (-not $SkipSelfTest) {
    $Result = Start-Process -FilePath $OutputPath -ArgumentList "--self-test" -WindowStyle Hidden -Wait -PassThru
    if ($Result.ExitCode -ne 0) { throw "桌面自检失败，退出码 $($Result.ExitCode)。" }
}

$File = Get-Item -LiteralPath $OutputPath
Write-Output ([pscustomobject]@{
    status = "ok"
    shell = "Tauri 2 + WebView2"
    product = "Agent4Company"
    path = $File.FullName
    bytes = $File.Length
    sha256 = (Get-FileHash -LiteralPath $File.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
} | ConvertTo-Json -Compress)
