<#
.SYNOPSIS
Builds the self-contained Windows app into the app folder.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Repository OWNER/REPOSITORY
#>
param([string]$Repository = "iamahmadyasin/gtvh-multimodal-analyzer", [string]$Branch = "main")

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$venv = "build\venv"
if (-not (Test-Path "$venv\Scripts\python.exe")) { python -m venv $venv }
& "$venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
& "$venv\Scripts\python.exe" -m pip install --quiet ".[build]"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& "$venv\Scripts\python.exe" -m PyInstaller packaging\gtvh.spec --noconfirm --distpath . --workpath build\pyinstaller
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
robocopy gtvh app\gtvh /E /XD __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
@{ repository = $Repository; branch = $Branch } | ConvertTo-Json | Set-Content -Encoding utf8 app\update.json
Remove-Item -Recurse -Force build, *.egg-info -ErrorAction SilentlyContinue
Write-Host "Built app\GTVH Multimodal Analyzer.exe (updates from: $(if ($Repository) { $Repository } else { 'not set' }))"
exit 0
