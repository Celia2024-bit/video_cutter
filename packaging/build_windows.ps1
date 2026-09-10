# Build a Windows folder app + optional Setup.exe (Inno Setup).
# From the repo root:
#   powershell -ExecutionPolicy Bypass -File packaging/build_windows.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Dist = Join-Path $Root "dist\VideoCutter"
$Cache = Join-Path $Root "packaging\cache"
$Venv = Join-Path $Root "packaging\.venv"
$Py = Join-Path $Venv "Scripts\python.exe"
New-Item -ItemType Directory -Force -Path $Cache | Out-Null

Write-Host "== build venv (only the packages this app needs) =="
if (-not (Test-Path $Py)) {
    python -m venv $Venv
}
& $Py -m pip install --upgrade pip
& $Py -m pip install flask groq faster-whisper pyinstaller

Write-Host "== ffmpeg =="
$FfmpegZip = Join-Path $Cache "ffmpeg-essentials.zip"
$FfmpegDir = Join-Path $Cache "ffmpeg"
$HaveFfmpeg = $false
if (Test-Path $FfmpegDir) {
    $HaveFfmpeg = [bool](Get-ChildItem -Path $FfmpegDir -Filter ffmpeg.exe -Recurse -ErrorAction SilentlyContinue)
}
if (-not $HaveFfmpeg) {
    $url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    Write-Host "Downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $FfmpegZip
    if (Test-Path $FfmpegDir) { Remove-Item $FfmpegDir -Recurse -Force }
    Expand-Archive -Path $FfmpegZip -DestinationPath $FfmpegDir -Force
}
$FfmpegExe = Get-ChildItem -Path $FfmpegDir -Filter ffmpeg.exe -Recurse | Select-Object -First 1
if (-not $FfmpegExe) { throw "ffmpeg.exe not found after download" }
$FfmpegBin = $FfmpegExe.Directory.FullName
Write-Host "Using ffmpeg in $FfmpegBin"

Write-Host "== PyInstaller =="
& $Py -m PyInstaller packaging/VideoCutter.spec --noconfirm --clean
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit $LASTEXITCODE" }

$FfmpegDest = Join-Path $Dist "ffmpeg"
New-Item -ItemType Directory -Force -Path $FfmpegDest | Out-Null
Copy-Item (Join-Path $FfmpegBin "ffmpeg.exe") $FfmpegDest -Force
Copy-Item (Join-Path $FfmpegBin "ffprobe.exe") $FfmpegDest -Force
$Ffplay = Join-Path $FfmpegBin "ffplay.exe"
if (Test-Path $Ffplay) {
    Copy-Item $Ffplay $FfmpegDest -Force
}
Copy-Item (Join-Path $Root "packaging\windows\Install-VideoCutter.cmd") $Dist -Force

$Zip = Join-Path $Root "dist\VideoCutter-windows.zip"
if (Test-Path $Zip) { Remove-Item $Zip -Force }
Write-Host "== zip $Zip =="
Compress-Archive -Path (Join-Path $Dist "*") -DestinationPath $Zip

Write-Host "App folder: $Dist"
$IsccCandidates = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
)
$Iscc = $IsccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($Iscc) {
    Write-Host "== Inno Setup ($Iscc) =="
    & $Iscc (Join-Path $Root "packaging\windows\VideoCutter.iss")
    if ($LASTEXITCODE -ne 0) { throw "ISCC failed with exit $LASTEXITCODE" }
    Write-Host "Installer: $(Join-Path $Root 'dist\VideoCutter-Setup.exe')"
} else {
    Write-Host "Inno Setup not found, skipping Setup.exe."
    Write-Host "Give people dist\VideoCutter-windows.zip, or install Inno Setup 6 and re-run."
}
