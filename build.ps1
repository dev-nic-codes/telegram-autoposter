$ErrorActionPreference = "Stop"

function Step($msg) {
  Write-Host "[build] $msg"
}

function Warn($msg) {
  Write-Host "[build] WARNING: $msg" -ForegroundColor Yellow
}

Step "Starting executable build..."

# Resolve key paths relative to this script so it works from any CWD.
$sourceDir = Resolve-Path $PSScriptRoot
$buildDir = Join-Path $sourceDir "build"
$distDir = Join-Path $sourceDir "dist"
$srcDir = Join-Path $sourceDir "src"
$mainPy = Join-Path $sourceDir "main.py"

# Prefer the project venv if it exists, otherwise fall back to the launcher.
$pythonExe = $null
$pythonArgs = @()

$venvPython = Join-Path $sourceDir ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
  $pythonExe = $venvPython
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
  $pythonExe = "py"
  $pythonArgs = @("-3")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
  $pythonExe = "python"
} else {
  throw "Python was not found. Run setup.bat first."
}

Step "Using Python: $pythonExe $($pythonArgs -join ' ')"

# Ensure PyInstaller is available.
try {
  & $pythonExe @pythonArgs -m PyInstaller --version | Out-Null
  Step "PyInstaller is available."
} catch {
  Warn "PyInstaller not found. Attempting to install..."
  & $pythonExe @pythonArgs -m pip install --upgrade pip | Out-Null
  & $pythonExe @pythonArgs -m pip install pyinstaller | Out-Null
  Step "PyInstaller installed."
}

# Clean old build outputs.
Step "Cleaning old build outputs..."
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $buildDir
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $distDir
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $sourceDir "release")
Get-ChildItem -Path $sourceDir -Filter "*.spec" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue

# Build into a tidy release folder.
Step "Building TelegramAutoposter.exe..."
& $pythonExe @pythonArgs -m PyInstaller `
  --noconfirm `
  --clean `
  --onefile `
  --name TelegramAutoposter `
  --console `
  --paths $srcDir `
  --hidden-import bot `
  --hidden-import commands `
  --hidden-import config `
  --hidden-import filters `
  --hidden-import media_handler `
  --hidden-import reddit_handler `
  --hidden-import scheduler `
  --hidden-import state_manager `
  --hidden-import telegram_handler `
  --hidden-import utils `
  --distpath $distDir `
  --workpath $buildDir `
  --specpath $buildDir `
  $mainPy

# Remove intermediate build files; keep release output only.
Step "Removing intermediate build folder..."
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $buildDir
Get-ChildItem -Path $sourceDir -Filter "*.spec" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue

$exePath = Join-Path $distDir "TelegramAutoposter.exe"
if (Test-Path $exePath) {
  Step "Build complete: $exePath"
} else {
  Warn "Build finished but dist\\TelegramAutoposter.exe was not found."
}
