param(
    [string]$PackageName = "",
    [switch]$KeepApiRunning
)

$ErrorActionPreference = "Stop"
$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendDir = Split-Path -Parent $DeployDir
$DataDir = Join-Path $BackendDir "data"
$DeltaDir = Join-Path $DataDir "skill-deltas"
$ComposeFile = Join-Path $DeployDir "docker-compose.yml"
$MemoryOverride = Join-Path $DeployDir "docker-compose.override.memory.yml"
$Project = "autoskill-collector"

if (-not $PackageName) {
    $PackageName = "skills-{0}.zip" -f (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
}
if ($PackageName -notmatch '^[A-Za-z0-9._-]+\.zip$') {
    throw "PackageName must be a simple .zip filename"
}

New-Item -ItemType Directory -Force -Path $DataDir, $DeltaDir, (Join-Path $BackendDir "skills_library") | Out-Null

if (-not $env:GITHUB_TOKEN) {
    Write-Warning "GITHUB_TOKEN is unset; discovery will use the intentionally small anonymous crawl budget."
}

if (-not $env:API_MEMORY_LIMIT) {
    # skill_delta.py export loads the whole active library into memory rather
    # than streaming it, so the collector's api needs more headroom than
    # production's 1536m default as the local corpus grows. Applied only via
    # docker-compose.override.memory.yml -- production's own compose file
    # keeps its literal 1536m, which compose_preflight.py enforces.
    $env:API_MEMORY_LIMIT = "4096m"
}
try {
    Write-Host "Building the isolated one-shot collector..."
    docker compose -p $Project -f $ComposeFile -f $MemoryOverride build api worker
    if ($LASTEXITCODE -ne 0) { throw "collector image build failed" }

    docker compose -p $Project -f $ComposeFile -f $MemoryOverride up -d api
    if ($LASTEXITCODE -ne 0) { throw "collector API failed to start" }

    $ready = $false
    foreach ($attempt in 1..60) {
        # A brand-new collector database is intentionally not route-ready yet;
        # wait only for the local REST process that the one-shot worker needs.
        docker compose -p $Project -f $ComposeFile exec -T api python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=5)" 2>$null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Seconds 3
    }
    if (-not $ready) { throw "collector API did not become ready" }

    Write-Host "Running one discovery and embedding pass. This can take a while."
    docker compose -p $Project -f $ComposeFile --profile collector run --rm -e GITHUB_TOKEN worker
    if ($LASTEXITCODE -ne 0) { throw "collector run failed; no package was exported" }

    docker compose -p $Project -f $ComposeFile exec -T api python skill_delta.py export `
        --db /data/local_skills.db `
        --library-dir /app/skills_library `
        --output "/data/skill-deltas/$PackageName"
    if ($LASTEXITCODE -ne 0) { throw "skill package export failed" }

    docker compose -p $Project -f $ComposeFile exec -T api python skill_delta.py validate "/data/skill-deltas/$PackageName"
    if ($LASTEXITCODE -ne 0) { throw "exported package did not validate" }

    Write-Host "Validated package: $(Join-Path $DeltaDir $PackageName)"
}
finally {
    if (-not $KeepApiRunning) {
        docker compose -p $Project -f $ComposeFile -f $MemoryOverride --profile collector down 2>$null | Out-Null
    }
}
