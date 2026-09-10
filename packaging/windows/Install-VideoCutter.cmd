@echo off
setlocal
set "SRC=%~dp0"
set "DEST=%LOCALAPPDATA%\Programs\VideoCutter"
echo Installing Video Cutter to:
echo   %DEST%
echo.

mkdir "%DEST%" 2>nul
xcopy /E /I /Y "%SRC%*" "%DEST%" >nul
if errorlevel 1 (
  echo Copy failed.
  pause
  exit /b 1
)

set "SHORTCUT=%USERPROFILE%\Desktop\Video Cutter.lnk"
set "STARTMENU=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Video Cutter.lnk"
powershell -NoProfile -Command ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%SHORTCUT%'); $s.TargetPath='%DEST%\VideoCutter.exe'; $s.WorkingDirectory='%DEST%'; $s.Save(); $s=(New-Object -ComObject WScript.Shell).CreateShortcut('%STARTMENU%'); $s.TargetPath='%DEST%\VideoCutter.exe'; $s.WorkingDirectory='%DEST%'; $s.Save()"

echo Desktop and Start Menu shortcuts created.
echo Launching Video Cutter...
start "" "%DEST%\VideoCutter.exe"
echo.
echo Done. Close the black window later to quit the app.
pause
