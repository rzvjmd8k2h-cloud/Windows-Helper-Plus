<#
.SYNOPSIS
    安装 Windows Helper Plus 的 SYSTEM 只读证据代理（需要管理员权限，一次即可）。

.DESCRIPTION
    - 把只读代理脚本复制到 %ProgramFiles%\WindowsHelperPlus\SystemBroker（管理员可写）；
    - 在 %ProgramData%\WindowsHelperPlus\SystemBroker 建立请求/响应队列，并只允许
      指定的普通用户提交请求、读取响应；
    - 注册一个以 NT AUTHORITY\SYSTEM 运行的计划任务，开机启动并常驻；
    - 代理只支持固定的 inspect_target / ping 操作，不执行任意命令、不修改系统。

.NOTES
    以管理员身份运行：
      powershell -ExecutionPolicy Bypass -File .\scripts\install_system_broker.ps1
    如果 UAC 使用了另一个管理员账户，请把当前普通用户的 SID 传进来：
      ... -OperatorSid S-1-5-21-...
#>
[CmdletBinding()]
param(
    [string]$OperatorSid = ''
)

$ErrorActionPreference = 'Stop'
$taskName = 'WindowsHelperPlusSystemBroker'
$projectRoot = Split-Path -Parent $PSScriptRoot
$brokerSource = Join-Path $PSScriptRoot 'system_broker.ps1'
$installDir = Join-Path $env:ProgramFiles 'WindowsHelperPlus\SystemBroker'
$installedBroker = Join-Path $installDir 'system_broker.ps1'
$dataRoot = Join-Path $env:ProgramData 'WindowsHelperPlus\SystemBroker'
$requestsDir = Join-Path $dataRoot 'requests'
$responsesDir = Join-Path $dataRoot 'responses'
$logsDir = Join-Path $dataRoot 'logs'
$powershellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$icacls = Join-Path $env:SystemRoot 'System32\icacls.exe'

function Assert-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw '此脚本需要管理员权限。请右键“以管理员身份运行”或在提升的 PowerShell 中执行。'
    }
    return $identity
}

function Invoke-Icacls([string[]]$Arguments) {
    & $icacls @Arguments | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "icacls 失败（退出码 $LASTEXITCODE）：$($Arguments -join ' ')"
    }
}

$identity = Assert-Admin
if (-not $OperatorSid) {
    # 如果 UAC 使用了另一个管理员账户，优先采用当前登录控制台用户的 SID；
    # 这样普通用户 UI 仍能访问请求/响应目录。
    try {
        $consoleUser = (Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction SilentlyContinue).UserName
        if ($consoleUser) {
            $OperatorSid = (New-Object System.Security.Principal.NTAccount($consoleUser)).Translate(
                [System.Security.Principal.SecurityIdentifier]
            ).Value
        }
    } catch { }
}
if (-not $OperatorSid) {
    $OperatorSid = $identity.User.Value
}
if ($OperatorSid -notmatch '^S-1-[0-9-]+$') {
    throw "OperatorSid 不合法：$OperatorSid"
}
if (-not (Test-Path -LiteralPath $brokerSource)) {
    throw "找不到代理脚本：$brokerSource"
}
if (-not (Test-Path -LiteralPath $powershellExe)) {
    throw "找不到 Windows PowerShell：$powershellExe"
}

Write-Host '正在保护安装目录并复制 SYSTEM 代理…'
New-Item -ItemType Directory -Path $installDir -Force | Out-Null
# Program Files 下的脚本只允许 Administrators/SYSTEM 写、Users 读。
Invoke-Icacls @(
    $installDir,
    '/inheritance:r',
    '/grant:r',
    '*S-1-5-18:(OI)(CI)F',
    '*S-1-5-32-544:(OI)(CI)F',
    '*S-1-5-32-545:(OI)(CI)RX'
)
Copy-Item -LiteralPath $brokerSource -Destination $installedBroker -Force

Write-Host '正在建立 ProgramData 请求/响应队列…'
foreach ($dir in @($dataRoot, $requestsDir, $responsesDir, $logsDir)) {
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
}
# 数据根：SYSTEM/管理员完全控制，操作员只读/遍历。
Invoke-Icacls @(
    $dataRoot,
    '/inheritance:r',
    '/grant:r',
    '*S-1-5-18:(OI)(CI)F',
    '*S-1-5-32-544:(OI)(CI)F',
    "*${OperatorSid}:(OI)(CI)(RX)"
)
# 请求目录：操作员可创建请求；响应/日志目录：操作员只读。
Invoke-Icacls @(
    $requestsDir,
    '/inheritance:r',
    '/grant:r',
    '*S-1-5-18:(OI)(CI)F',
    '*S-1-5-32-544:(OI)(CI)F',
    "*${OperatorSid}:(OI)(CI)M"
)
foreach ($readOnlyDir in @($responsesDir, $logsDir)) {
    Invoke-Icacls @(
        $readOnlyDir,
        '/inheritance:r',
        '/grant:r',
        '*S-1-5-18:(OI)(CI)F',
        '*S-1-5-32-544:(OI)(CI)F',
        "*${OperatorSid}:(OI)(CI)(RX)"
    )
}

Write-Host '正在注册 SYSTEM 计划任务…'
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}
$taskArgument = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}"' -f $installedBroker
$action = New-ScheduledTaskAction -Execute $powershellExe -Argument $taskArgument -WorkingDirectory $installDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable
Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description 'Windows Helper Plus read-only SYSTEM evidence broker (inspect_target only)' `
    -Force | Out-Null
Start-ScheduledTask -TaskName $taskName

Write-Host '等待 SYSTEM 代理心跳…'
$statusFile = Join-Path $responsesDir 'broker-status.json'
$brokerIdentity = ''
$deadline = (Get-Date).AddSeconds(20)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    if (Test-Path -LiteralPath $statusFile) {
        try {
            $status = Get-Content -LiteralPath $statusFile -Raw -Encoding UTF8 | ConvertFrom-Json
            $brokerIdentity = [string]$status.identity
            if ($brokerIdentity -match '(?i)(^|\\)SYSTEM$') { break }
        } catch { }
    }
}
if ($brokerIdentity -notmatch '(?i)(^|\\)SYSTEM$') {
    throw "SYSTEM 代理未能启动或身份不是 SYSTEM（当前：$brokerIdentity）。请检查计划任务 $taskName 和 $dataRoot\logs\broker.log。"
}

$installInfo = [ordered]@{
    version = 1
    installed_at = (Get-Date).ToString('o')
    operator_sid = $OperatorSid
    task_name = $taskName
    installed_broker = $installedBroker
    data_root = $dataRoot
    broker_identity = $brokerIdentity
}
$installInfoPath = Join-Path $dataRoot 'install-info.json'
$json = $installInfo | ConvertTo-Json -Depth 5
[IO.File]::WriteAllText($installInfoPath, $json, (New-Object Text.UTF8Encoding($false)))

Write-Host ''
Write-Host 'SYSTEM 只读代理已安装并运行。' -ForegroundColor Green
Write-Host "  身份：$brokerIdentity"
Write-Host "  计划任务：$taskName"
Write-Host "  安装脚本：$installedBroker"
Write-Host "  数据目录：$dataRoot"
Write-Host "  允许的操作员 SID：$OperatorSid"
Write-Host '代理只支持只读 inspect_target / ping，不执行命令、不修改系统。'
