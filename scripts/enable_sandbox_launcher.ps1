param()
$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $PSScriptRoot 'enable_windows_sandbox.ps1'
Write-Host '即将请求 UAC 管理员权限来启用 Windows Sandbox。'
Write-Host '启用后需要重启一次才会生效；本工具不会自动重启。'
try {
    $elevatedArgs = '-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $script
    Start-Process -FilePath 'powershell.exe' `
        -Verb RunAs `
        -ArgumentList $elevatedArgs `
        -Wait
} catch {
    Write-Host "UAC 提权失败或已取消：$($_.Exception.Message)" -ForegroundColor Yellow
}
Write-Host '结果见 Log\sandbox\enable-windows-sandbox-result.json'
