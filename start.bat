@echo off
cd /d "%~dp0"

set NOPAUSE=
if /i "%~1"=="--no-pause" set NOPAUSE=1

call "%~dp0_setup.bat"
if errorlevel 1 (
    call :maybepause
    exit /b 1
)

REM Een eventueel al draaiende EbookArr (venster of tray) afsluiten.
echo Eventuele oude EbookArr afsluiten...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8686 " ^| findstr LISTENING') do taskkill /f /pid %%p >nul 2>nul

echo.
echo EbookArr draait op http://localhost:8686
if not defined NOPAUSE echo Laat dit venster open staan terwijl je de app gebruikt.
echo.
venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8686

call :maybepause
exit /b 0

:maybepause
if not defined NOPAUSE pause
goto :eof
