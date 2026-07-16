param(
    [string]$ActorEmail = "neel.avalareddy@gmail.com",
    [string]$Reason = "Weekly automated skill delta import",
    [string]$RemoteHost = "157.245.168.172",
    [string]$RemoteUser = "root",
    [string]$RemoteKey = "$env:USERPROFILE\.ssh\id_ed25519",
    [string]$RemoteDir = "/opt/auto-skill-connector"
)

# Unattended weekly export+ship+apply. No human reviews the plan output
# before apply -- that review gate was traded away deliberately for
# automation; if that changes, split this back into export/transfer and a
# manual `apply-skill-delta.sh` run.

$ErrorActionPreference = "Stop"
$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendDir = Split-Path -Parent $DeployDir
$DeltaDir = Join-Path $BackendDir "data\skill-deltas"
$LogFile = Join-Path $BackendDir "data\weekly-skill-delta.log"

if ($ActorEmail -match "'" -or $Reason -match "'") {
    throw "ActorEmail/Reason must not contain a single quote (used unescaped in a remote shell command)"
}

function Log([string]$Message) {
    $line = "$(Get-Date -Format o)  $Message"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line
}

New-Item -ItemType Directory -Force -Path $DeltaDir | Out-Null

try {
    Log "=== weekly skill delta run starting ==="

    $apiRunning = docker ps --filter "name=autoskill-collector-api-1" --filter "status=running" -q
    if (-not $apiRunning) {
        throw "autoskill-collector-api-1 is not running; start run-collector-continuous.ps1 first"
    }

    $PackageName = "skills-{0}.zip" -f (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
    Log "Exporting package $PackageName"
    & (Join-Path $DeployDir "export-skill-delta.ps1") -PackageName $PackageName
    if ($LASTEXITCODE -ne 0) { throw "export-skill-delta.ps1 failed" }

    $LocalPackagePath = Join-Path $DeltaDir $PackageName
    $LocalHash = (Get-FileHash -Algorithm SHA256 -Path $LocalPackagePath).Hash.ToLower()
    Log "Local package sha256: $LocalHash"

    Log "Copying $PackageName to droplet"
    & scp -i $RemoteKey -o StrictHostKeyChecking=accept-new $LocalPackagePath "${RemoteUser}@${RemoteHost}:${RemoteDir}/backend/data/skill-deltas/$PackageName"
    if ($LASTEXITCODE -ne 0) { throw "scp to droplet failed" }

    $RemoteHashLine = & ssh -i $RemoteKey -o StrictHostKeyChecking=accept-new "${RemoteUser}@${RemoteHost}" "sha256sum ${RemoteDir}/backend/data/skill-deltas/$PackageName"
    if ($LASTEXITCODE -ne 0) { throw "could not hash package on droplet" }
    $RemoteHash = ($RemoteHashLine -split '\s+')[0]
    if ($RemoteHash -ne $LocalHash) {
        throw "hash mismatch after transfer: local=$LocalHash remote=$RemoteHash"
    }
    Log "Transfer verified: sha256 $RemoteHash"

    Log "Applying on droplet as $ActorEmail"
    & ssh -i $RemoteKey -o StrictHostKeyChecking=accept-new "${RemoteUser}@${RemoteHost}" `
        "cd ${RemoteDir} && bash backend/deploy/apply-skill-delta.sh $PackageName '$ActorEmail' '$Reason' --confirm"
    if ($LASTEXITCODE -ne 0) { throw "apply-skill-delta.sh failed on droplet" }

    Log "=== weekly skill delta run complete: $PackageName applied ==="
} catch {
    Log "ERROR: $($_.Exception.Message)"
    throw
}
