# Requires -Version 5.1
<#
.SYNOPSIS
    Start this repo's Temporal Worker against the official local Temporal.
    Config-root is mandatory. This script never starts a Temporal server.
.EXAMPLE
    .\scripts\start-worker.ps1 -ConfigRoot D:\pipeline-config
#>
[CmdletBinding()]
param(
    [string] $ConfigRoot = "",
    [string] $TemporalTarget = "127.0.0.1:7233",
    [string] $TaskQueue = "dev-pipeline",
    [switch] $SkipTemporalCheck,
    [switch] $Help
)

$ErrorActionPreference = "Stop"

function Write-Fail {
    param([string] $Message, [int] $Code)
    [Console]::Error.WriteLine($Message)
    exit $Code
}

function Show-Usage {
    @(
        "用法: .\scripts\start-worker.ps1 -ConfigRoot <已存在的绝对目录> [-TemporalTarget 127.0.0.1:7233] [-TaskQueue dev-pipeline]",
        "",
        "配置根目录是必填参数，不能省略，也不会回退到当前目录或默认路径。",
        "本仓库 Worker 默认连接已运行的官方 Temporal：127.0.0.1:7233（容器 agent-platform-temporal-1）。",
        "本脚本不会启动 Temporal。若 7233 没有服务，请到官方仓库启动 agent-platform 栈，不要在本仓库再起一份：",
        "",
        '  cd "D:\projects\kpl agents\agent-platform"',
        "  .\scripts\agent_platform_start.ps1",
        "",
        "不要使用 docker compose --profile temporal，也不要使用 temporal server start-dev。"
    ) | Write-Host
}

if ($Help) {
    Show-Usage
    exit 0
}

if ([string]::IsNullOrWhiteSpace($ConfigRoot)) {
    Show-Usage
    Write-Fail "配置根目录是必填参数，不能省略，也不会回退到当前目录或默认路径。请传入 -ConfigRoot。" 2
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# Validate the config-root in the script so a missing path fails clearly
# even when the host python is only a Windows Store stub.
if (-not [System.IO.Path]::IsPathRooted($ConfigRoot)) {
    Write-Fail "配置根目录必须是绝对路径，已拒绝相对路径: $ConfigRoot" 2
}
if (-not (Test-Path -LiteralPath $ConfigRoot)) {
    Write-Fail "配置根目录不存在: $ConfigRoot。不会当作「可能已存在」继续执行。" 2
}
if (-not (Test-Path -LiteralPath $ConfigRoot -PathType Container)) {
    Write-Fail "配置根目录不是目录: $ConfigRoot" 2
}

function Test-IsWindowsStorePython {
    param([string] $PythonPath)
    if ([string]::IsNullOrWhiteSpace($PythonPath)) { return $false }
    return $PythonPath -match '\\WindowsApps\\'
}

function Resolve-DevPipelinePython {
    $venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) {
        return , @($venvPython)
    }

    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $prev = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & py -3.12 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)" | Out-Null
        $code = $LASTEXITCODE
        $ErrorActionPreference = $prev
        if ($code -eq 0) {
            return , @("py", "-3.12")
        }
    }

    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd -and -not (Test-IsWindowsStorePython $pythonCmd.Source)) {
        return , @("python")
    }

    Write-Fail "未找到可用的 Python 3.12。Windows 商店里的 python 占位程序不能用。请先按 docs/dev-setup.md 用官方安装包创建 .venv，或用 Docker 跑测试：docker compose run --build --rm test" 2
}

if (-not $SkipTemporalCheck -and $TemporalTarget -eq "127.0.0.1:7233") {
    $reachable = $false
    $client = $null
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect("127.0.0.1", 7233, $null, $null)
        $reachable = $iar.AsyncWaitHandle.WaitOne(1000, $false) -and $client.Connected
    } catch {
        $reachable = $false
    } finally {
        if ($client) { $client.Close() }
    }
    if (-not $reachable) {
        $down = @(
            "本仓库 Worker 连接到已运行的官方 Temporal：127.0.0.1:7233（容器 agent-platform-temporal-1）。",
            "当前该地址没有服务。请到官方仓库启动 agent-platform 栈，不要在本仓库再起一份 Temporal，也不要使用 docker compose --profile temporal 或 temporal server start-dev。",
            "",
            '  cd "D:\projects\kpl agents\agent-platform"',
            "  .\scripts\agent_platform_start.ps1",
            "",
            "只需确认 Temporal 在 127.0.0.1:7233 监听。本期不要求对接 agent-platform 的 worker / app / init；不要尝试修复其 schema / init 失败。"
        ) -join [Environment]::NewLine
        Write-Fail $down 1
    }
}

$python = Resolve-DevPipelinePython
$exe = $python[0]
$prefix = @()
if ($python.Length -gt 1) {
    $prefix = $python[1..($python.Length - 1)]
}
& $exe @prefix -m dev_pipeline.worker --config-root $ConfigRoot --temporal-target $TemporalTarget --task-queue $TaskQueue
exit $LASTEXITCODE
