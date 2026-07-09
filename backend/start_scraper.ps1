$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot

$logPath = Join-Path $PSScriptRoot "scraper.log"
$env:PYTHONUNBUFFERED = "1"

function Write-Log {
    param([string]$Message)
    Add-Content -Path $logPath -Value "$(Get-Date -Format o) [start_scraper] $Message"
}

Write-Log "supervisor starting in $PSScriptRoot"

try {
    $env:GITHUB_TOKEN = (gh auth token 2>$null)
} catch {
    $env:GITHUB_TOKEN = ""
}
if ($env:GITHUB_TOKEN) {
    Write-Log "GitHub token loaded from gh auth"
} else {
    Write-Log "GitHub token unavailable; scraper will use unauthenticated GitHub limits"
}

# Storage moved local 2026-07-05 (Supabase free-tier space ran out) -- the
# scraper now writes to local_skills.db via local_api.py, no Supabase key
# needed. SUPABASE_SERVICE_KEY is unused as of this change.

# Set this to your self-hosted SearXNG instance (e.g. "http://localhost:8888")
# to enable general web search beyond GitHub/npm/registries. Leave unset to skip it.
if (-not $env:SEARXNG_URL) {
    $env:SEARXNG_URL = "http://localhost:8888"
}

Write-Log "SEARXNG_URL=$env:SEARXNG_URL; AUTO_START_SCRAPER=$env:AUTO_START_SCRAPER; LOCAL_DB_PATH=$env:LOCAL_DB_PATH"

# Google/GitHub OAuth apps backing account login (auth.py). Set these as
# persistent user/machine env vars (setx), or drop them in
# ~/.autoskill/backend_oauth.env (KEY=value per line, loaded here) -- either
# way they survive restarts of this supervisor. Unset means that provider's
# /auth/{provider}/start 503s instead of breaking anything else. See
# deploy/.env.example for the redirect URIs each provider's OAuth app must be
# registered with.
$oauthEnvPath = Join-Path $HOME ".autoskill\backend_oauth.env"
if (Test-Path -LiteralPath $oauthEnvPath) {
    foreach ($line in Get-Content -LiteralPath $oauthEnvPath) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#") -or -not $trimmed.Contains("=")) { continue }
        $key, $value = $trimmed.Split("=", 2)
        if (-not (Get-Item -Path "Env:$key" -ErrorAction SilentlyContinue)) {
            Set-Item -Path "Env:$key" -Value $value
        }
    }
    Write-Log "loaded OAuth credentials from $oauthEnvPath"
}
if ($env:GOOGLE_CLIENT_ID) {
    Write-Log "Google OAuth login is configured"
} else {
    Write-Log "GOOGLE_CLIENT_ID is not set; Google login will 503"
}
if ($env:GITHUB_CLIENT_ID) {
    Write-Log "GitHub OAuth login is configured for avalahome.com"
} else {
    Write-Log "GITHUB_CLIENT_ID is not set; GitHub login will 503 on avalahome.com"
}
# GitHub OAuth Apps only support one callback URL each, so serving login on
# both avalahome.com and autoskill.dev needs a second GitHub OAuth App --
# see auth.py's GITHUB_CREDENTIALS_BY_HOST and deploy/.env.example.
if ($env:GITHUB_CLIENT_ID_AUTOSKILL) {
    Write-Log "GitHub OAuth login is configured for autoskill.dev"
} else {
    Write-Log "GITHUB_CLIENT_ID_AUTOSKILL is not set; GitHub login will 503 on autoskill.dev"
}

while ($true) {
    Write-Log "starting python scraper.py"
    python "$PSScriptRoot\scraper.py" *>> $logPath
    Write-Log "process exited with code $LASTEXITCODE, restarting in 10s"
    Start-Sleep -Seconds 10
}
