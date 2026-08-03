$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot
$log = "$PSScriptRoot\backfill_packages_supervisor.log"

# backfill_packages.py only selects rows still missing package_hash and commits
# per row/page, so a killed run is always safe to restart from where it left off.
# Some rows can never succeed (no cached content, or content_hash no longer
# matches the cached copy) and will never leave the "remaining" set, so a run
# that makes no progress means everything left is permanently stuck -- stop
# instead of looping forever re-processing the same unresolvable rows.
$previousRemaining = $null
for ($i = 1; $i -le 500; $i++) {
    Add-Content -Path $log -Value "=== backfill_packages run $i $(Get-Date -Format o) ==="
    $remaining = (python _backfill_packages_remaining.py).Trim()
    Add-Content -Path $log -Value "remaining: $remaining"
    if ($remaining -eq "0") {
        Add-Content -Path $log -Value "backfill_packages: no candidate rows left, done"
        exit 0
    }
    if ($null -ne $previousRemaining -and $remaining -ge $previousRemaining) {
        Add-Content -Path $log -Value "backfill_packages: no progress since last run ($previousRemaining -> $remaining), $remaining rows are permanently unresolvable (no cached content or content_hash mismatch) -- stopping"
        exit 1
    }
    $previousRemaining = $remaining
    cmd /c "python -u backfill_packages.py >> `"$log`" 2>&1"
    Start-Sleep -Seconds 10
}
Add-Content -Path $log -Value "backfill_packages: gave up after 500 runs"
