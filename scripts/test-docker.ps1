# Requires -Version 5.1
<#
.SYNOPSIS
    Build the test image and run pytest, ruff, and mypy in Docker.
    With -WithTemporal, also start a Temporal dev server on localhost:7233
    and run the live History tests.
.EXAMPLE
    .\scripts\test-docker.ps1
    .\scripts\test-docker.ps1 -WithTemporal
#>
[CmdletBinding()]
param(
    [switch] $WithTemporal
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$docker = $null
if (Get-Command docker -ErrorAction SilentlyContinue) {
    $docker = "docker"
} elseif (Test-Path "C:\Users\lenovo\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe") {
    $docker = "C:\Users\lenovo\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe"
} else {
    Write-Error "未找到 docker。请先启动 Docker Desktop，并阅读 docs/dev-setup.md。"
    exit 2
}

& $docker compose version | Out-Host
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $docker compose build test
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& $docker compose run --rm test
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if ($WithTemporal) {
    & $docker compose --profile temporal run --rm test-history
    $code = $LASTEXITCODE
    & $docker compose --profile temporal down
    if ($code -ne 0) { exit $code }
}
