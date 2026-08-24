[CmdletBinding()]
param(
    [string]$Profile = "company-manager",
    [int]$Port = 8766
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Executable = Join-Path $ProjectRoot "Agent4Company.exe"

if ($Profile -notin @("company-manager", "company-with-sales")) {
    throw "未知工作台组合：$Profile"
}
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    throw "尚未生成 Agent4Company.exe，请先运行构建脚本。"
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "端口 $Port 已被占用，无法执行独立桌面验证。"
}

$env:AGENT4COMPANY_PROFILE = $Profile
$DesktopProcess = Start-Process -FilePath $Executable -WindowStyle Hidden -PassThru
$Health = $null
$Bootstrap = $null
try {
    for ($Attempt = 0; $Attempt -lt 40; $Attempt++) {
        try {
            $Health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
            $Bootstrap = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/bootstrap" -TimeoutSec 2
            break
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if ($null -eq $Health -or $null -eq $Bootstrap) {
        throw "桌面工作台未在 20 秒内就绪。"
    }
    $LibraryCapability = $Bootstrap.platform_capabilities |
        Where-Object { $_.id -eq "platform.library" } |
        Select-Object -First 1
    if ($null -eq $LibraryCapability) {
        throw "桌面工作台未加载公司资料库能力。"
    }
    $LibraryNavigation = $Bootstrap.navigation |
        Where-Object { $_.id -eq "knowledge" } |
        Select-Object -First 1
    if ($null -eq $LibraryNavigation) {
        throw "桌面工作台未提供公司资料库导航。"
    }
    [pscustomobject]@{
        status = "ok"
        product = $Health.product_id
        profile = $Health.profile_id
        desktop_route = $Health.desktop_route
        library_mode = $LibraryCapability.configuration_mode
        library_navigation = $LibraryNavigation.label
        process_running = -not $DesktopProcess.HasExited
    } | ConvertTo-Json -Compress
} finally {
    if (-not $DesktopProcess.HasExited) {
        $DesktopProcess.Refresh()
        $null = $DesktopProcess.CloseMainWindow()
        if (-not $DesktopProcess.WaitForExit(10000)) {
            throw "桌面主窗口未在 10 秒内正常退出，请手动关闭后再继续。"
        }
    }
}
