# Dot-sourced by every scripts\*.ps1 wrapper: finds the repo root and the venv
# python, and runs from the repo root (the tools and services read .env there).
# All logic lives in Python (tools\platform, tools\config) so it is testable.
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    # non-Windows layout (CI / smoke tests)
    $Python = Join-Path $RepoRoot ".venv/bin/python"
}
if (-not (Test-Path $Python)) {
    Write-Output "FAIL venv: no virtual environment at $RepoRoot\.venv"
    Write-Output "     FIX: py -3.12 -m venv .venv; .venv\Scripts\python -m pip install -e ."
    exit 1
}
Set-Location $RepoRoot
$env:PYTHONPATH = $RepoRoot
$env:PYTHONIOENCODING = "utf-8"
