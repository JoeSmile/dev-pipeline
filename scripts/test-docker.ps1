# Requires -Version 5.1
<#
.SYNOPSIS
    Build the test image and run pytest, ruff, and mypy in Docker.
    Use -LiveHistory to run live History tests against the official
    already-running Temporal at 127.0.0.1:7233. This script never starts
    a Temporal server.
.EXAMPLE
    .\scripts\test-docker.ps1
    .\scripts\test-docker.ps1 -LiveHistory
#>
[CmdletBinding()]
param(
    [switch] $LiveHistory,
    [switch] $WithTemporal
)

$ErrorActionPreference = "Stop"

function Write-Fail {
    param([string] $Message, [int] $Code)
    [Console]::Error.WriteLine($Message)
    exit $Code
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

if ($WithTemporal) {
    $msg = @(
        "不要用 -WithTemporal 再起一份 Temporal。本机正式服务已占用 127.0.0.1:7233（容器 agent-platform-temporal-1）。",
        "请改用 -LiveHistory，或：",
        "",
        "  docker compose run --build --rm -e TEMPORAL_TARGET=host.docker.internal:7233 test python -m pytest -m temporal_server -vv",
        "",
        "不要使用 docker compose --profile temporal，也不要使用 temporal server start-dev。",
        "若 7233 没有服务，请到 D:\projects\kpl agents\agent-platform 运行 .\scripts\agent_platform_start.ps1。"
    ) -join [Environment]::NewLine
    Write-Fail $msg 2
}

$docker = $null
if (Get-Command docker -ErrorAction SilentlyContinue) {
    $docker = "docker"
} elseif (Test-Path "C:\Users\lenovo\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe") {
    $docker = "C:\Users\lenovo\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe"
} else {
    Write-Fail "未找到 docker。请先启动 Docker Desktop，并阅读 docs/dev-setup.md。" 2
}

& $docker compose version | Out-Host
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if ($LiveHistory) {
    & $docker compose run --build --rm -e TEMPORAL_TARGET=host.docker.internal:7233 test python -m pytest -m temporal_server -vv
    exit $LASTEXITCODE
}

& $docker compose run --build --rm test
exit $LASTEXITCODE
