<# Installs a pre-commit hook that refuses to commit borrower documents, databases or backups. #>
$Root = Split-Path -Parent $PSScriptRoot
$hookDir = Join-Path $Root ".git\hooks"
if (-not (Test-Path $hookDir)) { Write-Host "Not a git checkout; skipping hook."; exit 0 }
Copy-Item (Join-Path $PSScriptRoot "git-hooks\pre-commit") (Join-Path $hookDir "pre-commit") -Force
Write-Host "Installed pre-commit hook (blocks PDFs, images, databases, backups and data folders)."
