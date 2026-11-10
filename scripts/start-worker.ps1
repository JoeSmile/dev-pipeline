# Requires -Version 5.1
<#
.SYNOPSIS
    Start the dev-pipeline Temporal worker. Config-root is mandatory.
.EXAMPLE
    .\scripts\start-worker.ps1 -ConfigRoot D:\pipeline-config
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $ConfigRoot,
    [string] $TemporalTarget = "localhost:7233",
    [string] $TaskQueue = "dev-pipeline"
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ConfigRoot)) {
    Write-Error "配置根目录是必填参数，不能省略，也不会回退到当前目录或默认路径。请传入 -ConfigRoot。"
    exit 2
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$python = "python"
if (Get-Command python -ErrorAction SilentlyContinue) {
    $python = "python"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $python = "py"
} else {
    Write-Error "未找到 Python。请先安装 Python 3.12，并阅读 docs/dev-setup.md。"
    exit 2
}

& $python -m dev_pipeline.worker --config-root $ConfigRoot --temporal-target $TemporalTarget --task-queue $TaskQueue
exit $LASTEXITCODE
