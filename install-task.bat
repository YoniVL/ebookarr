@echo off
cd /d "%~dp0"
echo.
echo === EbookArr op de achtergrond installeren ===
echo.
echo EbookArr start dan automatisch (met een tray-icoon, geen venster) telkens
echo als je je aanmeldt. Afsluiten kan via rechtsklik op het tray-icoon.
echo.

call "%~dp0_setup.bat"
if errorlevel 1 (
    echo.
    echo Setup is mislukt - er is niets geinstalleerd.
    pause
    exit /b 1
)

echo.
echo Snelkoppeling in de opstartmap zetten...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$w = New-Object -ComObject WScript.Shell;" ^
  "$lnk = $w.CreateShortcut([Environment]::GetFolderPath('Startup') + '\EbookArr.lnk');" ^
  "$lnk.TargetPath = '%~dp0venv\Scripts\pythonw.exe';" ^
  "$lnk.Arguments = '-m app.tray';" ^
  "$lnk.WorkingDirectory = '%~dp0'.TrimEnd('\');" ^
  "$lnk.IconLocation = '%~dp0venv\Scripts\pythonw.exe,0';" ^
  "$lnk.Save()"
if errorlevel 1 goto faal

REM Een eventueel al draaiende versie afsluiten en nu starten.
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8686 " ^| findstr LISTENING') do taskkill /f /pid %%p >nul 2>nul
echo Nu starten...
start "" "venv\Scripts\pythonw.exe" -m app.tray

echo.
echo Klaar. Zoek het EbookArr-icoontje bij je systeemtray (naast de klok,
echo eventueel onder het pijltje "verborgen pictogrammen").
echo Open na een halve minuut http://localhost:8686
echo.
echo Ongedaan maken kan met uninstall-task.bat
echo.
pause
exit /b 0

:faal
echo.
echo FOUT: het aanmaken van de snelkoppeling is mislukt.
echo.
pause
exit /b 1
