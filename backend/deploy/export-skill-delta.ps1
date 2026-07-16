param(
    [string]$PackageName = ""
)

$ErrorActionPreference = "Stop"
$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendDir = Split-Path -Parent $DeployDir
$DeltaDir = Join-Path $BackendDir "data/skill-deltas"
$ComposeFile = Join-Path $DeployDir "docker-compose.yml"
$Project = "autoskill-collector"

if (-not $PackageName) {
    $PackageName = "skills-{0}.zip" -f (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
}
if ($PackageName -notmatch '^[A-Za-z0-9._-]+\.zip$') {
    throw "PackageName must be a simple .zip filename"
}

New-Item -ItemType Directory -Force -Path $DeltaDir | Out-Null

docker compose -p $Project -f $ComposeFile exec -T api python skill_delta.py export `
    --db /data/local_skills.db `
    --library-dir /app/skills_library `
    --output "/data/skill-deltas/$PackageName"
if ($LASTEXITCODE -ne 0) { throw "skill package export failed" }

docker compose -p $Project -f $ComposeFile exec -T api python skill_delta.py validate "/data/skill-deltas/$PackageName"
if ($LASTEXITCODE -ne 0) { throw "exported package did not validate" }

Write-Host "Validated package: $(Join-Path $DeltaDir $PackageName)"
Write-Host "Ship it to the droplet, then run apply-skill-delta.sh there."
