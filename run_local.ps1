# immi-watch - office-PC runner.
# Checks the sources marked `runner: local` in sources.yaml (sites that block GitHub's cloud
# servers), raises the same GitHub Issue alerts, and pushes snapshots to the shared repo.
# When the cloud check has stopped running, it also covers the cloud's sources (see below).
# Scheduled hourly by Windows Task Scheduler (task "immi-watch local check"), launched hidden
# through run_local.vbs. Log: local-run.log next to this script.

Set-Location -LiteralPath $PSScriptRoot
$log = Join-Path $PSScriptRoot "local-run.log"
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 1MB)) {
    Get-Content $log -Tail 2000 | Set-Content "$log.tmp" -Encoding utf8
    Move-Item "$log.tmp" $log -Force
}
function Log($msg) { $msg | Out-File -FilePath $log -Append -Encoding utf8 }

Log "=== $(Get-Date -Format 's') local run"
git pull -q --rebase 2>&1 | ForEach-Object { Log "$_" }

$env:IMMI_RUNNER = "local"
$env:GITHUB_REPOSITORY = "Rafidk9/immi-watch"
$env:ALERT_MENTION = "Rafidk9"
$env:GITHUB_TOKEN = (gh auth token 2>$null)
$env:PYTHONIOENCODING = "utf-8"

& python monitor.py 2>&1 | ForEach-Object { Log "$_" }
$ok = ($LASTEXITCODE -eq 0)
$paths = @("snapshots", "changes", "state-local.json")

# Failover: if the GitHub cloud check has not completed a run for over 3 hours (e.g. Actions
# disabled by a billing lock), check its sources from this PC too. It stops by itself once the
# cloud check runs again, because that updates last_run in state.json.
$cloudAge = $null
try {
    $cloudLast = (Get-Content state.json -Raw -Encoding utf8 | ConvertFrom-Json).last_run.at
    $cloudAge = ((Get-Date) - [datetimeoffset]::Parse($cloudLast).LocalDateTime).TotalHours
} catch { Log "could not read cloud last_run: $_" }
if ($ok -and $cloudAge -gt 3) {
    Log ("cloud check last ran {0:N1} h ago - checking its sources from this PC" -f $cloudAge)
    $env:IMMI_RUNNER = "actions"
    & python monitor.py 2>&1 | ForEach-Object { Log "$_" }
    if ($LASTEXITCODE -eq 0) { $paths += "state.json" } else { $ok = $false }
}
Remove-Item Env:GITHUB_TOKEN -ErrorAction SilentlyContinue

if ($ok) {
    git add -A @paths 2>&1 | ForEach-Object { Log "$_" }
    git diff --cached --quiet
    if ($LASTEXITCODE -ne 0) {
        git commit -q -m "local check $(Get-Date -Format 'yyyy-MM-dd HH:mm')" 2>&1 | ForEach-Object { Log "$_" }
        git pull -q --rebase 2>&1 | ForEach-Object { Log "$_" }
        if (Test-Path .git/rebase-merge) {
            # Cloud and PC both saved the same files this hour: keep the cloud's copy, retry next run.
            git rebase --abort 2>&1 | ForEach-Object { Log "$_" }
            git reset -q --hard "@{u}" 2>&1 | ForEach-Object { Log "$_" }
            Log "rebase conflict with the cloud check; dropped this run's local commit"
        } else {
            git push -q 2>&1 | ForEach-Object { Log "$_" }
        }
    }
} else {
    Log "monitor.py failed; snapshots not pushed"
}

# Backstop for GitHub's cron, which can skip or delay scheduled runs: if the cloud check hasn't
# run for over 65 minutes, start it now.
try {
    $last = gh run list --repo Rafidk9/immi-watch --workflow monitor.yml --limit 1 --json createdAt --jq '.[0].createdAt' 2>$null
    if ($last) {
        $age = ((Get-Date).ToUniversalTime() - [datetime]::Parse($last).ToUniversalTime()).TotalMinutes
        if ($age -gt 65) {
            gh workflow run monitor.yml --repo Rafidk9/immi-watch 2>&1 | ForEach-Object { Log "$_" }
            Log ("GitHub check last ran {0:N0} min ago - started it" -f $age)
        }
    }
} catch { Log "backstop check failed: $_" }
