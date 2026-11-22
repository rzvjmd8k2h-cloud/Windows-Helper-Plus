# 一键创建本地虚拟环境并安装运行依赖（仅修改项目目录，不修改系统）。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1
#   powershell -ExecutionPolicy Bypass -File .\setup.ps1 -UseTsinghuaMirror
#
param(
    [switch]$UseTsinghuaMirror
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$pythonExe = $null
$pythonPrefix = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -c "import sys" 2>$null
    if ($LASTEXITCODE -eq 0) {
        $pythonExe = 'py'
        $pythonPrefix = @('-3')
    }
}
if (-not $pythonExe -and (Get-Command python -ErrorAction SilentlyContinue)) {
    & python -c "import sys" 2>$null
    if ($LASTEXITCODE -eq 0) {
        $pythonExe = 'python'
    }
}
if (-not $pythonExe) {
    throw "未找到 Python。请安装 Python 3.10+（建议 3.12–3.14）并加入 PATH。"
}

$versionText = & $pythonExe @pythonPrefix -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
Write-Host "使用 Python $versionText"
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    Write-Host "创建 .venv …"
    & $pythonExe @pythonPrefix -m venv .venv
} else {
    Write-Host ".venv 已存在，复用。"
}

$pipArgs = @('-m', 'pip', 'install', '--upgrade', 'pip')
if ($UseTsinghuaMirror) {
    $pipArgs += @('-i', 'https://pypi.tuna.tsinghua.edu.cn/simple')
}
& .\.venv\Scripts\python.exe @pipArgs

$installArgs = @('-m', 'pip', 'install', '-r', 'requirements.txt')
if ($UseTsinghuaMirror) {
    $installArgs += @('-i', 'https://pypi.tuna.tsinghua.edu.cn/simple')
}
& .\.venv\Scripts\python.exe @installArgs

Write-Host ""
Write-Host "依赖安装完成。" -ForegroundColor Green
Write-Host "启动界面：.\run.bat"
Write-Host "首次使用：在“设置”中保存 DeepSeek API Key（所有分析必须经过 AI）。"
