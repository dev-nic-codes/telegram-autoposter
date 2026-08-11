param(
  [switch]$SkipConfigure
)

$ErrorActionPreference = "Stop"

function Step($msg) {
  Write-Host "[setup] $msg"
}

function Warn($msg) {
  Write-Host "[setup] WARNING: $msg" -ForegroundColor Yellow
}

Step "Starting automated setup..."

# Resolve paths relative to this script so it works from any CWD.
$sourceDir = Resolve-Path $PSScriptRoot
$requirementsPath = Join-Path $sourceDir "requirements.txt"
$venvDir = Join-Path $sourceDir ".venv"

Push-Location $sourceDir

# 1) Locate Python
$pythonExe = $null
$pythonArgs = @()

if (Get-Command py -ErrorAction SilentlyContinue) {
  $pythonExe = "py"
  $pythonArgs = @("-3.12")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
  $pythonExe = "python"
} else {
  throw "Python was not found. Install Python 3.12 and re-run setup.ps1."
}

Step "Using Python launcher: $pythonExe $($pythonArgs -join ' ')"

# 2) Create venv if needed
$venvPython = Join-Path $venvDir "Scripts\\python.exe"
if (-not (Test-Path $venvPython)) {
  Step "Creating virtual environment (.venv)..."
  & $pythonExe @pythonArgs -m venv $venvDir
} else {
  Step "Virtual environment already exists."
}

if (-not (Test-Path $venvPython)) {
  throw "Virtual environment was not created successfully."
}

# 3) Install Python dependencies
Step "Upgrading pip/setuptools/wheel..."
& $venvPython -m pip install --upgrade pip setuptools wheel

Step "Installing requirements.txt..."
& $venvPython -m pip install -r $requirementsPath

# 4) Ensure ffmpeg is available
function Ensure-Ffmpeg {
  if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    Step "ffmpeg is already available."
    return
  }

  Step "ffmpeg not found. Attempting to install..."

  $installed = $false

  if (Get-Command winget -ErrorAction SilentlyContinue) {
    try {
      Step "Installing ffmpeg via winget (Gyan.FFmpeg)..."
      winget install --id Gyan.FFmpeg -e --accept-package-agreements --accept-source-agreements | Out-Host
      $installed = $true
    } catch {
      Warn "winget install failed: $($_.Exception.Message)"
    }
  } elseif (Get-Command choco -ErrorAction SilentlyContinue) {
    try {
      Step "Installing ffmpeg via Chocolatey..."
      choco install ffmpeg -y | Out-Host
      $installed = $true
    } catch {
      Warn "Chocolatey install failed: $($_.Exception.Message)"
    }
  } else {
    Warn "Neither winget nor choco is available to auto-install ffmpeg."
  }

  # Try to locate winget-installed ffmpeg and add it to PATH for this session
  if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    $wingetBase = Join-Path $env:LOCALAPPDATA "Microsoft\\WinGet\\Packages"
    if (Test-Path $wingetBase) {
      $ffDirs = Get-ChildItem -Path $wingetBase -Directory -Filter "Gyan.FFmpeg*" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending
      foreach ($dir in $ffDirs) {
        $bin = Join-Path $dir.FullName "ffmpeg\\bin"
        if (Test-Path $bin) {
          Step "Adding ffmpeg to PATH for this session: $bin"
          $env:PATH = "$bin;$env:PATH"
          break
        }
      }
    }
  }

  if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    Step "ffmpeg is available."
  } else {
    Warn "ffmpeg is still not available. Install it manually and re-open the terminal."
  }
}

Ensure-Ffmpeg

if (-not (Test-Path -LiteralPath (Join-Path $sourceDir "config.json"))) {
  Copy-Item -LiteralPath (Join-Path $sourceDir "config.example.json") -Destination (Join-Path $sourceDir "config.json")
}

if (-not $SkipConfigure) {
  & $venvPython (Join-Path $sourceDir "setup_wizard.py")
}

Step "Setup complete."
Write-Host ""
Write-Host "Next steps:"
Write-Host "1) Configure the bot with:"
Write-Host "   .\.venv\Scripts\python.exe .\setup_wizard.py"
Write-Host "2) Validate the installation with:"
Write-Host "   .\.venv\Scripts\python.exe .\scripts\doctor.py"
Write-Host "3) Start unattended mode with:"
Write-Host "   .\start.ps1"
Write-Host ""
Write-Host "Optional: activate the venv first:"
Write-Host "   .\\.venv\\Scripts\\Activate.ps1"

Pop-Location
