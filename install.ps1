$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

Write-Host "Setting up Python virtual environment..."
if (-not (Test-Path -LiteralPath ".venv")) {
    python -m venv .venv
}

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment Python was not found at $Python"
}

Write-Host "Installing Python dependencies..."
& $Python -m pip install --upgrade pip
& $Python -m pip install -r requirements.txt

Write-Host "Checking FFmpeg..."
$Ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
if ($Ffmpeg) {
    Write-Host "FFmpeg found at $($Ffmpeg.Source)"
} else {
    $Winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($Winget) {
        Write-Host "FFmpeg was not found. Installing Gyan.FFmpeg with winget..."
        winget install --id Gyan.FFmpeg -e --accept-package-agreements --accept-source-agreements
        Write-Host "If ffmpeg is still not found, close this terminal and open a new one so PATH is refreshed."
    } else {
        Write-Host "winget is not available. Install FFmpeg manually:"
        Write-Host "1. Download a Windows build from https://www.gyan.dev/ffmpeg/builds/"
        Write-Host "2. Extract it, for example to C:\ffmpeg"
        Write-Host "3. Add C:\ffmpeg\bin to your Windows Path"
        Write-Host "4. Open a new terminal and run: ffmpeg -version"
    }
}

Write-Host ""
Write-Host "Setup complete."
Write-Host "Copy .env.example to .env, edit the values, then run:"
Write-Host ".\.venv\Scripts\uvicorn.exe main:app --host 0.0.0.0 --port 8000 --env-file .env"
