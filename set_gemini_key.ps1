# Saves the Gemini API key for the office-PC runner, so its alerts get AI briefings too.
# Run once:  powershell -ExecutionPolicy Bypass -File set_gemini_key.ps1
# The key goes into .env next to this script (git-ignored; it never leaves this PC).

Set-Location -LiteralPath $PSScriptRoot
$secure = Read-Host "Paste your Gemini API key (it won't show) and press Enter" -AsSecureString
$key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)).Trim()
if (-not $key) { Write-Host "No key entered; nothing saved."; exit 1 }

$lines = @()
if (Test-Path .env) { $lines = @(Get-Content .env -Encoding utf8 | Where-Object { $_ -notmatch '^\s*GEMINI_API_KEY\s*=' }) }
$lines += "GEMINI_API_KEY=$key"
Set-Content .env $lines -Encoding utf8
Write-Host "Saved. Alerts from this PC will include AI briefings from the next hourly run."
