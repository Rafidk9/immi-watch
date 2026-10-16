# immi-watch - office-PC runner.
# Checks the sources marked `runner: local` in sources.yaml (sites that block GitHub's cloud
# servers), raises the same GitHub Issue alerts, and pushes snapshots to the shared repo.
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
Remove-Item Env:GITHUB_TOKEN -ErrorAction SilentlyContinue

if ($ok) {
    git add -A snapshots changes state-local.json 2>&1 | ForEach-Object { Log "$_" }
    git diff --cached --quiet
    if ($LASTEXITCODE -ne 0) {
        git commit -q -m "local check $(Get-Date -Format 'yyyy-MM-dd HH:mm')" 2>&1 | ForEach-Object { Log "$_" }
        git pull -q --rebase 2>&1 | ForEach-Object { Log "$_" }
        git push -q 2>&1 | ForEach-Object { Log "$_" }
    }
} else {
    Log "monitor.py failed; snapshots not pushed"
}
