@echo off
cd /d "%~dp0"
echo.
echo EbookArr's automatische start bij aanmelden verwijderen.
echo De app en je gegevens blijven staan; start.bat blijft werken.
echo.

REM Snelkoppeling uit de opstartmap halen.
powershell -NoProfile -Command "Remove-Item ([Environment]::GetFolderPath('Startup') + '\EbookArr.lnk') -ErrorAction SilentlyContinue"

REM Oude variant (scheduled task) voor de zekerheid ook opruimen.
schtasks /end /tn "EbookArr" >nul 2>nul
schtasks /delete /tn "EbookArr" /f >nul 2>nul

echo Automatische start verwijderd.

REM De nu draaiende achtergrond-app afsluiten.
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8686 " ^| findstr LISTENING') do taskkill /f /pid %%p >nul 2>nul
echo De draaiende achtergrond-app is afgesloten.

echo.
pause
