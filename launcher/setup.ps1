# One-time installation of Kokoro PDF Reader. Everything is installed inside the project folder:
#   .runtime\python  - a private Python 3.12
#   .venv            - the application's libraries (PySide6, PyMuPDF, ONNX Runtime GPU, CUDA 12 / cuDNN 9 DLLs)
# Nothing is installed system-wide and NVIDIA drivers / CUDA installations are not touched.
# The speech model is downloaded later by the application itself, with a progress window.

param([switch]$NoShortcut)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Say($text, $color = "Gray") { Write-Host $text -ForegroundColor $color }

Say "Kokoro PDF Reader - first-time setup" "Cyan"
Say "Folder: $root"
Say ""

# 1. uv (the tool that installs Python and the libraries)
$uv = $null
$cmd = Get-Command uv -ErrorAction SilentlyContinue
if ($cmd) { $uv = $cmd.Source }
foreach ($candidate in @("$env:USERPROFILE\.local\bin\uv.exe", "$root\.runtime\uv\uv.exe")) {
    if (-not $uv -and (Test-Path $candidate)) { $uv = $candidate }
}
if (-not $uv) {
    Say "Downloading the installer tool (uv) into the project folder..." "Yellow"
    $env:UV_INSTALL_DIR = "$root\.runtime\uv"
    $env:UV_NO_MODIFY_PATH = "1"
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $uv = "$root\.runtime\uv\uv.exe"
    if (-not (Test-Path $uv)) { throw "Could not install uv. Check the internet connection and try again." }
}
Say "Using uv: $uv"

# 2. Private Python 3.12 inside the project
$env:UV_PYTHON_INSTALL_DIR = "$root\.runtime\python"
$env:UV_PYTHON_PREFERENCE = "only-managed"
Say ""
Say "Installing a private copy of Python 3.12..." "Yellow"
& $uv python install 3.12 --no-bin
if ($LASTEXITCODE -ne 0) { throw "Python installation failed." }

# 3. Application libraries, exactly the versions in uv.lock
Say ""
Say "Installing the application's libraries (about 2 GB, mostly NVIDIA CUDA libraries)..." "Yellow"
& $uv sync --frozen
if ($LASTEXITCODE -ne 0) { throw "Installing the libraries failed. Check the internet connection and run the launcher again." }

# 4. Report the graphics card
Say ""
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($smi) {
    $gpu = & nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
    Say "NVIDIA GPU found: $gpu" "Green"
} else {
    Say "No NVIDIA GPU driver found - speech will run on the processor (CPU)." "Yellow"
}

# 5. Desktop shortcut
if (-not $NoShortcut) { try {
    $desktop = [Environment]::GetFolderPath("Desktop")
    $shell = New-Object -ComObject WScript.Shell
    # WScript cannot save to some non-ASCII paths (e.g. a Hebrew OneDrive desktop), so save to temp and move
    $tmpLnk = Join-Path $env:TEMP "KokoroPdfReader.lnk"
    $lnk = $shell.CreateShortcut($tmpLnk)
    $lnk.TargetPath = "$root\Kokoro PDF Reader.bat"
    $lnk.WorkingDirectory = $root
    $lnk.WindowStyle = 7
    if (Test-Path "$root\launcher\app.ico") { $lnk.IconLocation = "$root\launcher\app.ico" }
    $lnk.Description = "Read PDF books and articles aloud"
    $lnk.Save()
    Move-Item -LiteralPath $tmpLnk -Destination (Join-Path $desktop "Kokoro PDF Reader.lnk") -Force
    Say "Created a desktop shortcut: Kokoro PDF Reader" "Green"
} catch {
    Say "Could not create a desktop shortcut (not important): $_" "Yellow"
} }

Say ""
Say "Setup finished." "Green"
exit 0
