$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot
$log = "$PSScriptRoot\backfill_capability_summary_supervisor.log"

# backfill_capability_summary.py only selects rows still missing capability_summary
# and commits per page (40 rows), so a killed run is always safe to restart from
# where it left off. Some rows will always get an empty summary+triggers back from
# the LLM and can never leave the "remaining" set, so if a full restart makes no
# progress at all, everything left is permanently stuck -- stop instead of looping
# forever re-running LLM calls over the same unresolvable rows.
$previousRemaining = $null
for ($i = 1; $i -le 2000; $i++) {
    Add-Content -Path $log -Value "=== backfill_capability_summary run $i $(Get-Date -Format o) ==="
    $remaining = (python _backfill_capability_summary_remaining.py).Trim()
    Add-Content -Path $log -Value "remaining: $remaining"
    if ($remaining -eq "0") {
        Add-Content -Path $log -Value "backfill_capability_summary: no candidate rows left, done"
        exit 0
    }
    if ($null -ne $previousRemaining -and $remaining -ge $previousRemaining) {
        Add-Content -Path $log -Value "backfill_capability_summary: no progress since last run ($previousRemaining -> $remaining), $remaining rows are permanently unresolvable (LLM returned empty summary+triggers) -- stopping"
        exit 1
    }
    $previousRemaining = $remaining
    # Keep concurrent LLM requests bounded; each page batches embeddings after
    # the requests, so higher fan-out only increases pressure without improving
    # the single-model local embedding path.
    cmd /c "python -u backfill_capability_summary.py --concurrency 2 >> `"$log`" 2>&1"
    Start-Sleep -Seconds 20
}
Add-Content -Path $log -Value "backfill_capability_summary: gave up after 2000 runs"
