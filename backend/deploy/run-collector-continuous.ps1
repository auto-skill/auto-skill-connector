param(
    [switch]$Stop
)

$ErrorActionPreference = "Stop"
$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendDir = Split-Path -Parent $DeployDir
$ComposeFile = Join-Path $DeployDir "docker-compose.yml"
$MemoryOverride = Join-Path $DeployDir "docker-compose.override.memory.yml"
$ContinuousOverride = Join-Path $DeployDir "docker-compose.override.continuous.yml"
$Project = "autoskill-collector"
$WorkerContainer = "autoskill-local-collector-worker"

if ($Stop) {
    Write-Host "Stopping $WorkerContainer ..."
    docker stop $WorkerContainer 2>$null | Out-Null
    docker compose -p $Project -f $ComposeFile -f $ContinuousOverride --profile collector rm -f worker 2>$null | Out-Null
    Write-Host "Stopped. The collector API/database are left running; use collect-skills.ps1 or export-skill-delta.ps1 -Stop separately if you want those down too."
    return
}

New-Item -ItemType Directory -Force -Path (Join-Path $BackendDir "data"), (Join-Path $BackendDir "data/skill-deltas"), (Join-Path $BackendDir "skills_library") | Out-Null

if (-not $env:GITHUB_TOKEN) {
    Write-Warning "GITHUB_TOKEN is unset; discovery will use the intentionally small anonymous crawl budget."
}

Write-Host "Building the isolated collector image..."
docker compose -p $Project -f $ComposeFile -f $MemoryOverride build api worker
if ($LASTEXITCODE -ne 0) { throw "collector image build failed" }

if (-not $env:API_MEMORY_LIMIT) {
    # skill_delta.py export loads the whole active library into memory rather
    # than streaming it, so the collector's api needs more headroom than
    # production's 1536m default as the local corpus grows. Applied only via
    # docker-compose.override.memory.yml -- production's own compose file
    # keeps its literal 1536m, which compose_preflight.py enforces.
    $env:API_MEMORY_LIMIT = "4096m"
}
docker compose -p $Project -f $ComposeFile -f $MemoryOverride up -d api
if ($LASTEXITCODE -ne 0) { throw "collector API failed to start" }

docker compose -p $Project -f $ComposeFile -f $ContinuousOverride --profile collector up -d worker
if ($LASTEXITCODE -ne 0) { throw "collector worker failed to start" }

Write-Host ""
Write-Host "Running continuously as Docker container: $WorkerContainer"
Write-Host "This is the ONLY safe name to look for -- it never runs as a bare"
Write-Host "host python.exe, so Windows Task Manager / Get-Process should never"
Write-Host "show it and should never be used to stop it."
Write-Host ""
Write-Host "  Check status : docker ps --filter name=$WorkerContainer"
Write-Host "  Tail logs     : docker logs -f $WorkerContainer"
Write-Host "  Stop it       : .\run-collector-continuous.ps1 -Stop"
Write-Host "                  (or: docker stop $WorkerContainer)"
Write-Host ""
Write-Host "It scrapes/embeds into the collector's own local DB every"
Write-Host "SCRAPE_INTERVAL_SECONDS (default 3600s). Run export-skill-delta.ps1"
Write-Host "whenever you want to ship a reviewed package to the droplet -- it"
Write-Host "does not stop this worker."
