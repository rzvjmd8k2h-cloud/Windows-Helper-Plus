<#
.SYNOPSIS
    安装 Windows Helper Plus 本地 Qwen 开机自启动/预检。

.DESCRIPTION
    在当前用户的“启动”文件夹创建快捷方式；登录时由 pythonw 运行
    scripts\local_qwen_service.py --autostart。
    - 启动 llama-server（本地 Qwen3-1.7B Q8 CPU）；
    - 预热模型并检查健康；
    - 检查缺少 Markdown 的旧日志，写入 Log/local-model/startup-status.json；
    - 不删除任何历史；不自动执行任何诊断/修复。
#>
[CmdletBinding()]
param(
    [string]$ProjectRoot = '',
    [switch]$TranslatePending
)
$ErrorActionPreference = 'Stop'
if (-not $ProjectRoot) { $ProjectRoot = Split-Path -Parent $PSScriptRoot }
$pythonw = Join-Path $ProjectRoot '.venv\Scripts\pythonw.exe'
$script = Join-Path $ProjectRoot 'scripts\local_qwen_service.py'
if (-not (Test-Path -LiteralPath $pythonw)) { throw "未找到 pythonw：$pythonw" }
if (-not (Test-Path -LiteralPath $script)) { throw "未找到服务脚本：$script" }

$startup = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startup 'WindowsHelperPlusLocalQwen.lnk'
$arguments = '"{0}" --autostart' -f $script
if ($TranslatePending) { $arguments += ' --translate-pending 1' }

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = $arguments
$shortcut.WorkingDirectory = $ProjectRoot
$shortcut.WindowStyle = 7
$shortcut.Description = 'Windows Helper Plus 本地 Qwen 上下文预检'
$shortcut.Save()
Write-Host "已安装开机自启动：$shortcutPath" -ForegroundColor Green
Write-Host '登录后会启动/预热带本地 Qwen；不会执行诊断或修复。'
Write-Host '移除：scripts\uninstall_local_qwen_autostart.ps1'
