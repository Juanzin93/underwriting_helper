<#
.SYNOPSIS  Back up the database, documents and reports to a ZIP (store it on encrypted media).
.EXAMPLE   scripts\backup.ps1 -Out D:\EncryptedDrive\uwhelper-backup.zip
#>
param([string]$Out = "")
$Root = Split-Path -Parent $PSScriptRoot
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
if ($Out) { & $VenvPy -m uwhelper backup --out $Out } else { & $VenvPy -m uwhelper backup }
