<#
.SYNOPSIS
    启用 Windows Sandbox（需要管理员；本脚本不自动重启）。

.DESCRIPTION
    启用 Containers-DisposableClientVM 可选功能及其依赖。
    执行后通常需要重启一次；重启前不会生效。
    本脚本只启用功能，不创建/启动沙箱，不修改其他系统设置。

    必须由用户在 UAC 中明确同意；脚本自身不做提权绕过。
#>
[CmdletBinding()]
param(
    [string]$ProjectRoot = '',
    [switch]$NoRestart
)
$ErrorActionPreference = 'Continue'
if (-not $ProjectRoot) { $ProjectRoot = Split-Path -Parent $PSScriptRoot }
$logDir = Join-Path $ProjectRoot 'Log\sandbox'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'enable-windows-sandbox.log'
$result = Join-Path $logDir 'enable-windows-sandbox-result.json'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
$isAdmin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    @{ ok = $false; error = '需要管理员权限；请通过 UAC 提权运行。'; at = (Get-Date).ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath $result -Encoding UTF8
    Write-Error '需要管理员权限'
    exit 5
}

$dismOutput = & DISM.exe /Online /Enable-Feature /FeatureName:Containers-DisposableClientVM /All /NoRestart 2>&1 | Out-String
$dismExit = $LASTEXITCODE
$dismOutput | Set-Content -LiteralPath $log -Encoding UTF8

$featureState = $null
try {
    $featureState = (Get-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM -ErrorAction Stop).State
} catch {
    $featureState = "query-failed: $($_.Exception.Message)"
}
$exePresent = Test-Path -LiteralPath (Join-Path $env:SystemRoot 'System32\WindowsSandbox.exe')
$success = ($dismExit -eq 0 -or $dismExit -eq 3010) -and "$featureState" -eq 'Enabled'
@{ ok = $success; reboot_required = ($dismExit -eq 3010 -or -not $exePresent); dism_exit = $dismExit; feature_state = "$featureState"; windows_sandbox_exe = $exePresent; at = (Get-Date).ToString('o'); note = '通常需要重启后 WindowsSandbox.exe 才会出现/生效' } |
    ConvertTo-Json | Set-Content -LiteralPath $result -Encoding UTF8

Write-Host "DISM exit: $dismExit (3010 = 成功，需要重启)"
Write-Host "Feature state: $featureState"
Write-Host "WindowsSandbox.exe present: $exePresent"
Write-Host "需要重启后才能使用 Windows Sandbox。"
if (-not $success) { exit $dismExit }
