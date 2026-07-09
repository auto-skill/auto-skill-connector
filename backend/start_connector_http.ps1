$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

$logPath = Join-Path $PSScriptRoot "connector_http.log"
$env:PYTHONUNBUFFERED = "1"

function Write-Log {
    param([string]$Message)
    Add-Content -Path $logPath -Value "$(Get-Date -Format o) [start_connector_http] $Message"
}

Write-Log "supervisor starting in $PSScriptRoot"

$env:MCP_TRANSPORT = "streamable-http"
if (-not $env:MCP_PORT) { $env:MCP_PORT = "8765" }
# Exposed at both https://mcp.autoskill.dev/mcp (canonical, "autoskill-dev"
# tunnel -- see ~/.cloudflared/autoskill-dev-config.yml) and
# https://mcp.avalahome.com/mcp (legacy "auto-skill" tunnel, kept only so
# existing tool calls don't break during the migration). Both must be listed
# here for Host-header DNS-rebinding protection to accept either.
$env:MCP_ALLOWED_HOSTS = "mcp.autoskill.dev,mcp.avalahome.com,localhost:8765,127.0.0.1:8765"
# autoskill.dev is the canonical user-facing domain: OAuth metadata and the
# login-chooser redirect must never surface avalahome.com to users. Pinned
# explicitly so a supervisor restart can't silently drift.
if (-not $env:MCP_ISSUER_URL) { $env:MCP_ISSUER_URL = "https://mcp.autoskill.dev" }
if (-not $env:BACKEND_BASE_URL) { $env:BACKEND_BASE_URL = "https://skills.autoskill.dev" }
# Skills server and connector run on the same box; the public backend
# hostname doesn't necessarily resolve correctly from this machine's own LAN
# (split-horizon DNS -- see README's LAN self-hosting note), so talk to it
# directly over loopback instead.
if (-not $env:AUTOSKILL_URL) { $env:AUTOSKILL_URL = "http://localhost:8000" }
Write-Log "MCP_TRANSPORT=$env:MCP_TRANSPORT; MCP_PORT=$env:MCP_PORT; AUTOSKILL_URL=$env:AUTOSKILL_URL; MCP_ALLOWED_HOSTS=$env:MCP_ALLOWED_HOSTS; MCP_ISSUER_URL=$env:MCP_ISSUER_URL; BACKEND_BASE_URL=$env:BACKEND_BASE_URL"

$connectorCandidates = @()
if ($env:AUTO_SKILL_CONNECTOR_DIR) {
    $connectorCandidates += $env:AUTO_SKILL_CONNECTOR_DIR
}
$connectorCandidates += @(
    # This script's own repo root -- covers running it directly from
    # auto-skill-connector\backend (the normal case; mcp_server.py lives one
    # level up from this file).
    (Split-Path -Path $PSScriptRoot -Parent),
    # Legacy sibling-checkout layout, kept only for backward compatibility.
    (Join-Path $PSScriptRoot "..\auto-skill-connector"),
    (Join-Path $PSScriptRoot "..\..\Skills"),
    (Join-Path ([Environment]::GetFolderPath("MyDocuments")) "Skills")
)

$connectorDir = $null
foreach ($candidate in $connectorCandidates) {
    if (-not $candidate) { continue }
    $serverPath = Join-Path $candidate "mcp_server.py"
    if (Test-Path -LiteralPath $serverPath) {
        $connectorDir = (Resolve-Path -LiteralPath $candidate).Path
        break
    }
}

if (-not $connectorDir) {
    Write-Log "connector checkout not found. Set AUTO_SKILL_CONNECTOR_DIR to the auto-skill-connector repo. Checked: $($connectorCandidates -join '; ')"
    throw "Connector checkout not found. Set AUTO_SKILL_CONNECTOR_DIR to the auto-skill-connector repo."
}
Write-Log "using connector checkout $connectorDir"

while ($true) {
    Write-Log "starting python mcp_server.py"
    python "$connectorDir\mcp_server.py" *>> $logPath
    Write-Log "process exited with code $LASTEXITCODE, restarting in 5s"
    Start-Sleep -Seconds 5
}
