<#
.SYNOPSIS  Start UW Helper on http://127.0.0.1:8765 and open the browser.
.EXAMPLE   scripts\start.ps1            scripts\start.ps1 -Model qwen3:4b -Port 8800
#>
param([string]$Model = "", [int]$Port = 0, [switch]$NoBrowser)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) { throw "Run scripts\setup.ps1 first." }
$args2 = @("-m", "uwhelper", "serve")
if ($Model) { $args2 += @("--model", $Model) }
if ($Port -gt 0) { $args2 += @("--port", $Port) } else { $Port = 8765 }
if (-not $NoBrowser) { Start-Job -ScriptBlock { param($p) Start-Sleep 3; Start-Process "http://127.0.0.1:$p/" } -ArgumentList $Port | Out-Null }
& $VenvPy @args2
