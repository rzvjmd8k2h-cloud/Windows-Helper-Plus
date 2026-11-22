<#
.SYNOPSIS
    移除 Windows Helper Plus 的所有资源管理器右键菜单项。
#>
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$paths = @(
    '*',
    'AllFilesystemObjects',
    'Directory',
    'Folder',
    'Drive',
    'Directory\Background',
    'DesktopBackground\Shell',
    'CLSID\{20D04FE0-3AEA-1069-A2D8-08002B30309D}\shell',
    'CLSID\{2227A280-3AEA-1069-A2DE-08002B30309D}\shell',
    'System.Devices.PrintQueue\shell',
    'System.Devices.PnPDevice\shell'
)
$removed = 0
foreach ($sub in $paths) {
    $keyPath = Join-Path 'HKCU:\Software\Classes' ($sub + '\shell\WindowsHelperPlus')
    if (Test-Path -LiteralPath $keyPath) {
        Remove-Item -LiteralPath $keyPath -Recurse -Force
        $removed++
        Write-Host "已移除：$sub"
    }
}
Write-Host "共移除 $removed 个菜单项。" -ForegroundColor Green
