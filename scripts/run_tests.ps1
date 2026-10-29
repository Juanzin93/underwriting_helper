<#
.SYNOPSIS  Run the automated tests. -Ollama also runs live tests against your local model.
#>
param([switch]$Ollama)
$Root = Split-Path -Parent $PSScriptRoot
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
Set-Location $Root
if ($Ollama) { $env:UWH_RUN_OLLAMA_TESTS = "1" }
& $VenvPy -m pytest -v
