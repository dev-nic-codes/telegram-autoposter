$ErrorActionPreference = "Stop"

function Step($msg) {
  Write-Host "[clean] $msg"
}

Step "Cleaning project data and temporary files..."

# Resolve paths relative to this script.
$sourceDir = Resolve-Path $PSScriptRoot

# Wipe saved data.
Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $sourceDir "config.json"), (Join-Path $sourceDir "state.json")
Get-ChildItem -Path $sourceDir -Filter "config.json.bak_*" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
Get-ChildItem -Path $sourceDir -Filter "state.json.backup_*" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue

# Remove Python caches.
Get-ChildItem -Path $sourceDir -Recurse -Directory -Filter __pycache__ -ErrorAction SilentlyContinue |
  Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# Remove build artifacts.
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $sourceDir "build"), (Join-Path $sourceDir "dist"), (Join-Path $sourceDir "release")
Get-ChildItem -Path $sourceDir -Filter "*.spec" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue

Step "Clean complete."
