# immi-watch - office-PC runner. Runs every check (GitHub Actions is no longer used to run them).
# Checks all sources in sources.yaml in two passes, each with its own state file:
#   1. `runner: local` sources (sites that block cloud servers)      -> state-local.json
#   2. all other sources (formerly checked by GitHub Actions)        -> state.json
# raises GitHub Issue alerts (GitHub emails them), rebuilds the status page at _site/index.html,
# and pushes snapshots to the shared repo as a backup and history.
# Scheduled hourly by Windows Task Scheduler (task "immi-watch local check"), launched hidden
# through run_local.vbs. Log: local-run.log next to this script. Nothing is checked while this
# PC is off or asleep.

Set-Location -LiteralPath $PSScriptRoot
$log = Join-Path $PSScriptRoot "local-run.log"
if ((Test-Path $log) -and ((Get-Item $log).Length -gt 1MB)) {
    Get-Content $log -Tail 2000 | Set-Content "$log.tmp" -Encoding utf8
    Move-Item "$log.tmp" $log -Force
}
function Log($msg) { $msg | Out-File -FilePath $log -Append -Encoding utf8 }

Log "=== $(Get-Date -Format 's') local run"
git pull -q --rebase 2>&1 | ForEach-Object { Log "$_" }

$env:GITHUB_REPOSITORY = "Rafidk9/immi-watch"
$env:ALERT_MENTION = "Rafidk9"
$env:GITHUB_TOKEN = (gh auth token 2>$null)
$env:PYTHONIOENCODING = "utf-8"
# Private settings for this PC (e.g. GEMINI_API_KEY=... for AI briefings), saved by
# set_gemini_key.ps1. The file is git-ignored, so the key never leaves this PC.
if (Test-Path .env) {
    foreach ($line in Get-Content .env -Encoding utf8) {
        if ($line -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.+?)\s*$') {
            Set-Item -Path "Env:$($Matches[1])" -Value $Matches[2]
        }
    }
}

# Pass 1 and 2. If either pass fails, nothing is pushed, so no change is saved without its
# alert; the next run re-checks.
$ok = $true
foreach ($runner in "local", "actions") {
    $env:IMMI_RUNNER = $runner
    & python monitor.py 2>&1 | ForEach-Object { Log "$_" }
    if ($LASTEXITCODE -ne 0) { $ok = $false; Log "monitor.py ($runner) failed" }
}
Remove-Item Env:GITHUB_TOKEN -ErrorAction SilentlyContinue

# Status page, viewable on this PC (open _site/index.html; git-ignored).
& python status_page.py 2>&1 | ForEach-Object { Log "$_" }

if ($ok) {
    git add -A snapshots changes state-local.json state.json 2>&1 | ForEach-Object { Log "$_" }
    git diff --cached --quiet
    if ($LASTEXITCODE -ne 0) {
        git commit -q -m "local check $(Get-Date -Format 'yyyy-MM-dd HH:mm')" 2>&1 | ForEach-Object { Log "$_" }
        git pull -q --rebase 2>&1 | ForEach-Object { Log "$_" }
        if (Test-Path .git/rebase-merge) {
            # Someone else pushed the same files: keep theirs, re-check next run.
            git rebase --abort 2>&1 | ForEach-Object { Log "$_" }
            git reset -q --hard "@{u}" 2>&1 | ForEach-Object { Log "$_" }
            Log "rebase conflict; dropped this run's local commit"
        } else {
            git push -q 2>&1 | ForEach-Object { Log "$_" }
        }
    }
} else {
    Log "a check pass failed; snapshots not pushed"
}
