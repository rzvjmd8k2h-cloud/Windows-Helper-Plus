<#
.SYNOPSIS
    卸载 Windows Helper Plus 的 SYSTEM 只读证据代理（需要管理员权限）。

.NOTES
    默认同时删除 ProgramData 下的代理队列和日志；以及 Program Files 下的代理脚本。
#>
[CmdletBinding()]
param(
    [switch]$KeepData
)

$ErrorActionPreference = 'Stop'
$taskName = 'WindowsHelperPlusSystemBroker'
$installDir = Join-Path $env:ProgramFiles 'WindowsHelperPlus\SystemBroker'
$dataRoot = Join-Path $env:ProgramData 'WindowsHelperPlus\SystemBroker'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw '此脚本需要管理员权限。请右键“以管理员身份运行”或在提升的 PowerShell 中执行。'
}

$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 400
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "已停止并删除计划任务：$taskName" -ForegroundColor Green
} else {
    Write-Host "未找到计划任务：$taskName" -ForegroundColor Yellow
}

if (Test-Path -LiteralPath $installDir) {
    Remove-Item -LiteralPath $installDir -Recurse -Force
    Write-Host "已删除安装目录：$installDir"
}
if (-not $KeepData -and (Test-Path -LiteralPath $dataRoot)) {
    Remove-Item -LiteralPath $dataRoot -Recurse -Force
    Write-Host "已删除代理数据目录：$dataRoot"
} elseif ($KeepData) {
    Write-Host "保留代理数据目录：$dataRoot"
}
Write-Host 'SYSTEM 只读代理已卸载。' -ForegroundColor Green
