@echo off
REM Gedeelde setup: controleert Python, maakt de venv en installeert de packages.
REM Wordt aangeroepen door start.bat en install-task.bat.
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo FOUT: Python is niet gevonden. Installeer het van https://www.python.org/downloads/
    echo en vink tijdens installatie "Add python.exe to PATH" aan.
    exit /b 1
)

if not exist "venv\Scripts\python.exe" (
    echo Eerste keer: virtuele omgeving aanmaken...
    rmdir /s /q venv 2>nul
    python -m venv venv
    if not exist "venv\Scripts\python.exe" (
        echo FOUT: het aanmaken van de virtuele omgeving is mislukt.
        exit /b 1
    )
)

echo Packages controleren/installeren...
venv\Scripts\python.exe -m pip install -r requirements.txt --disable-pip-version-check -q
if errorlevel 1 (
    echo FOUT: pip install is mislukt. Zie de foutmelding hierboven.
    exit /b 1
)

exit /b 0
