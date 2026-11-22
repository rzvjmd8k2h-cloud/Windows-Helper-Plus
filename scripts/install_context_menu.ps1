<#
.SYNOPSIS
    为 Windows Helper Plus 注册“全方位”资源管理器右键菜单（HKCU，无需管理员）。

.DESCRIPTION
    覆盖：
      - 任意文件 (*)
      - 所有文件系统对象 (AllFilesystemObjects)
      - 文件夹 / 驱动器 (Directory / Folder / Drive)
      - 文件夹背景 (Directory\Background)
      - 桌面背景 (DesktopBackground)
      - “此电脑”和设备选择器 (This PC CLSID)
      - “打印机”文件夹/打印队列 (Printers CLSID / System.Devices.PrintQueue)
    文件/目录/驱动器会传入选中路径并自动开始 AI 分析；
    设备和打印机菜单会打开对象选择器，选择后自动开始分析。
#>
[CmdletBinding()]
param(
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$launcher = Join-Path $projectRoot 'run_gui.pyw'
$menuText = '用 Windows Helper Plus 分析'
$commandPrefix = '"{0}" "{1}"' -f $pythonw, $launcher

# Path 为 HKCU\Software\Classes 下的相对路径；占位符替换由 Explorer 完成。
$entries = @(
    @{ Path = '*'; Args = '--auto-diagnose "%1"'; Desc = '任意文件' },
    @{ Path = 'AllFilesystemObjects'; Args = '--auto-diagnose "%1"'; Desc = '所有文件系统对象' },
    @{ Path = 'Directory'; Args = '--auto-diagnose "%1"'; Desc = '文件夹' },
    @{ Path = 'Folder'; Args = '--auto-diagnose "%1"'; Desc = '文件夹（兼容类）' },
    @{ Path = 'Drive'; Args = '--auto-diagnose "%1"'; Desc = '驱动器' },
    @{ Path = 'Directory\Background'; Args = '--auto-diagnose "%V"'; Desc = '文件夹空白处' },
    @{ Path = 'DesktopBackground\Shell'; Args = '--auto-diagnose'; Desc = '桌面空白处' },
    @{ Path = 'CLSID\{20D04FE0-3AEA-1069-A2D8-08002B30309D}\shell'; Args = '--pick-target device --auto-diagnose'; Desc = '此电脑（设备选择）' },
    @{ Path = 'CLSID\{2227A280-3AEA-1069-A2DE-08002B30309D}\shell'; Args = '--pick-target printer --auto-diagnose'; Desc = '打印机文件夹' },
    @{ Path = 'System.Devices.PrintQueue\shell'; Args = '--pick-target printer --auto-diagnose'; Desc = '打印队列对象（实验性）' },
    @{ Path = 'System.Devices.PnPDevice\shell'; Args = '--pick-target device --auto-diagnose'; Desc = 'PnP 设备对象（实验性）' }
)

function Get-MenuKeyPath {
    param([string]$BasePath)
    # DesktopBackground\Shell、CLSID\...\shell、System.Devices.*\shell 本身已经是菜单容器，
    # 不能再追加一层 shell，否则会得到 shell\shell 而不会显示。
    if ($BasePath -match '\\[Ss]hell$') {
        return (Join-Path 'HKCU:\Software\Classes' ($BasePath + '\WindowsHelperPlus'))
    }
    return (Join-Path 'HKCU:\Software\Classes' ($BasePath + '\shell\WindowsHelperPlus'))
}

function Get-LegacyKeyPath {
    param([string]$BasePath)
    return (Join-Path 'HKCU:\Software\Classes' ($BasePath + '\shell\WindowsHelperPlus'))
}

function Remove-HelperKeys {
    foreach ($entry in $entries) {
        $keyPath = Get-MenuKeyPath $entry.Path
        $legacyPath = Get-LegacyKeyPath $entry.Path
        foreach ($path in @($keyPath, $legacyPath) | Select-Object -Unique) {
            if (Test-Path -LiteralPath $path) {
                Remove-Item -LiteralPath $path -Recurse -Force
                Write-Host "已移除：$($entry.Desc)"
            }
        }
    }
    $oldPath = 'HKCU:\Software\Classes\*\shell\WindowsHelperPlus'
    if (Test-Path -LiteralPath $oldPath) {
        Remove-Item -LiteralPath $oldPath -Recurse -Force
    }
}

if ($Uninstall) {
    Remove-HelperKeys
    Write-Host 'Windows Helper Plus 右键菜单已移除。' -ForegroundColor Green
    return
}

if (-not (Test-Path -LiteralPath $pythonw)) {
    throw "未找到 pythonw.exe：$pythonw。请先运行 setup.ps1。"
}
if (-not (Test-Path -LiteralPath $launcher)) {
    throw "未找到 run_gui.pyw：$launcher"
}

foreach ($entry in $entries) {
    $keyPath = Get-MenuKeyPath $entry.Path
    $legacyPath = Get-LegacyKeyPath $entry.Path
    if ($legacyPath -ne $keyPath -and (Test-Path -LiteralPath $legacyPath)) {
        Remove-Item -LiteralPath $legacyPath -Recurse -Force
    }
    $command = $commandPrefix + ' ' + $entry.Args
    New-Item -Path $keyPath -Force | Out-Null
    New-Item -Path (Join-Path $keyPath 'command') -Force | Out-Null
    Set-ItemProperty -LiteralPath $keyPath -Name '(default)' -Value $menuText
    Set-ItemProperty -LiteralPath $keyPath -Name 'MUIVerb' -Value $menuText
    Set-ItemProperty -LiteralPath $keyPath -Name 'Icon' -Value $pythonw
    Set-ItemProperty -LiteralPath $keyPath -Name 'MultiSelectModel' -Value 'Single'
    Set-ItemProperty -LiteralPath (Join-Path $keyPath 'command') -Name '(default)' -Value $command
    Write-Host ("已注册：{0} -> {1}" -f $entry.Desc, $entry.Path) -ForegroundColor Green
}

Write-Host ''
Write-Host '全方位右键菜单注册完成（HKCU，无需管理员）。' -ForegroundColor Green
Write-Host 'Windows 11 精简菜单中请点击“显示更多选项”，或按 Shift+F10。'
Write-Host '文件夹/驱动器/桌面空白处也会出现菜单；设备/打印机入口打开对象选择器。'
Write-Host '移动项目目录后请重新运行本脚本。'
