<#
.SYNOPSIS
    移除 Windows Helper Plus 本地 Qwen 开机自启动快捷方式。
#>
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$startup = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startup 'WindowsHelperPlusLocalQwen.lnk'
if (Test-Path -LiteralPath $shortcutPath) {
    Remove-Item -LiteralPath $shortcutPath -Force
    Write-Host "已移除：$shortcutPath" -ForegroundColor Green
} else {
    Write-Host '未找到本地 Qwen 启动快捷方式。'
}
