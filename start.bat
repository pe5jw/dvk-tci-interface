@echo off
title DVK-TCI Interface
cd /d "%~dp0"

REM --- Controleer Python ---
python --version >nul 2>&1
if errorlevel 1 (
    echo [FOUT] Python niet gevonden. Installeer Python 3.10+ van python.org
    pause
    exit /b 1
)

REM --- Installeer afhankelijkheden als nodig ---
if not exist "venv\Scripts\activate.bat" (
    echo [INFO] Virtuele omgeving aanmaken...
    python -m venv venv
    call venv\Scripts\activate.bat
    pip install -q websockets
    echo [INFO] Klaar.
) else (
    call venv\Scripts\activate.bat
)

REM --- DVK map aanmaken ---
if not exist "dvk_wav" mkdir dvk_wav

echo.
echo  ================================================
echo   DVK-TCI Interface  --  PE5JW 2026
echo   N1MM+ ^<-^> TCI Server  (TS-2000 CAT + DVK)
echo  ================================================
echo  Configuratie: config.ini
echo  Stoppen: Ctrl+C
echo.

python src\dvk_tci_interface.py

if errorlevel 1 (
    echo.
    echo [FOUT] DVK-TCI Interface gestopt met een fout. Zie melding hierboven.
    pause
)
