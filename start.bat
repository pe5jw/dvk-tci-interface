@echo off
title DVK-TCI Interface v1.0 (console)
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo [FOUT] Python niet gevonden.
    pause & exit /b 1
)

if not exist "venv\Scripts\activate.bat" (
    echo [INFO] Installeren...
    python -m venv venv
    call venv\Scripts\activate.bat
    pip install -q websockets
) else (
    call venv\Scripts\activate.bat
)

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
if errorlevel 1 ( echo. & echo [FOUT] Zie melding. & pause )
