<#
.SYNOPSIS
  One-time setup for UW Helper on Windows 10/11.
.DESCRIPTION
  Creates a virtual environment, installs dependencies, initialises the local
  database, checks Ollama/Tesseract, and (optionally) pulls the local model.
  Internet is needed only for this step (pip packages, model download).
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -Model qwen3:4b -PullModel
#>
param(
    [string]$Model = "qwen3:8b",
    [switch]$PullModel,
    [switch]$SkipTests
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
Write-Host "== UW Helper setup in $Root ==" -ForegroundColor Cyan

# 1. Python 3.11+
$py = Get-Command py -ErrorAction SilentlyContinue
if ($py) { $PythonExe = "py"; $PyArgs = @("-3") } else { $PythonExe = "python"; $PyArgs = @() }
$ver = & $PythonExe @PyArgs -c "import sys; print('%d.%d' % sys.version_info[:2])"
if ([version]$ver -lt [version]"3.11") { throw "Python 3.11+ required (found $ver). Install from https://www.python.org/downloads/windows/" }
Write-Host "Python $ver OK"

# 2. Virtual environment + dependencies
if (-not (Test-Path ".venv")) { & $PythonExe @PyArgs -m venv .venv }
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
& $VenvPy -m pip install --upgrade pip
& $VenvPy -m pip install -r requirements-dev.txt
& $VenvPy -m pip install -e . --no-deps
Write-Host "Dependencies installed" -ForegroundColor Green

# 3. Git hook that blocks committing borrower documents / databases
if (Test-Path ".git") { & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "install-git-hooks.ps1") }

# 4. Database + policies
& $VenvPy -m uwhelper init

# 5. Tesseract
$tess = Get-Command tesseract -ErrorAction SilentlyContinue
if (-not $tess -and -not (Test-Path "C:\Program Files\Tesseract-OCR\tesseract.exe")) {
    Write-Warning "Tesseract not found. Scanned PDFs need it. Install with:  winget install --id UB-Mannheim.TesseractOCR"
    Write-Warning "or download from https://github.com/UB-Mannheim/tesseract/wiki , then reopen PowerShell."
} else { Write-Host "Tesseract found" -ForegroundColor Green }

# 6. Ollama + model
$ollama = Get-Command ollama -ErrorAction SilentlyContinue
if (-not $ollama) {
    Write-Warning "Ollama not found. Install with:  winget install --id Ollama.Ollama   (or https://ollama.com/download/windows)"
} else {
    if ($PullModel) { & ollama pull $Model }
    $list = & ollama list 2>$null
    if ($list -notmatch [regex]::Escape($Model)) { Write-Warning "Model $Model not downloaded yet. Run:  ollama pull $Model" }
    else { Write-Host "Model $Model present locally" -ForegroundColor Green }
}
if ($Model -ne "qwen3:8b") {
    $cfgDir = Join-Path $env:LOCALAPPDATA "UWHelper"
    New-Item -ItemType Directory -Force -Path $cfgDir | Out-Null
    # WriteAllText writes UTF-8 without a BOM (PowerShell 5 "Out-File -Encoding utf8" adds one, which TOML rejects).
    [IO.File]::WriteAllText((Join-Path $cfgDir "uwhelper.toml"), "ollama_model = `"$Model`"`n")
    Write-Host "Configured model $Model in $cfgDir\uwhelper.toml"
}

# 7. Tests (no Ollama or internet required)
if (-not $SkipTests) { & $VenvPy -m pytest -q }

& $VenvPy -m uwhelper doctor
Write-Host "`nSetup complete. Start the app with:  scripts\start.ps1" -ForegroundColor Cyan
