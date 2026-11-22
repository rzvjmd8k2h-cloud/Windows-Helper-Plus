<#
.SYNOPSIS
    禁用 Windows Sandbox（回滚；需要管理员；不自动重启）。
#>
[CmdletBinding()]
param([string]$ProjectRoot = '')
$ErrorActionPreference = 'Continue'
if (-not $ProjectRoot) { $ProjectRoot = Split-Path -Parent $PSScriptRoot }
$logDir = Join-Path $ProjectRoot 'Log\sandbox'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$output = & DISM.exe /Online /Disable-Feature /FeatureName:Containers-DisposableClientVM /NoRestart 2>&1 | Out-String
$output | Set-Content -LiteralPath (Join-Path $logDir 'disable-windows-sandbox.log') -Encoding UTF8
@{ ok = ($LASTEXITCODE -eq 0); dism_exit = $LASTEXITCODE; output = $output; at = (Get-Date).ToString('o') } |
    ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $logDir 'disable-windows-sandbox-result.json') -Encoding UTF8
Write-Host $output
Write-Host '需要重启后完全禁用。'
exit $LASTEXITCODE
