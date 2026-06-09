@echo off
title DVK-TCI Interface v1.0
cd /d "%~dp0"
python --version >nul 2>&1
if errorlevel 1 (echo [FOUT] Python niet gevonden && pause && exit /b 1)
if not exist "venv\Scripts\activate.bat" (
    echo [INFO] Installeren...
    python -m venv venv
    call venv\Scripts\activate.bat
    pip install -q websockets sounddevice numpy
) else (
    call venv\Scripts\activate.bat
    pip show sounddevice >nul 2>&1
    if errorlevel 1 pip install -q sounddevice numpy
)
if not exist "dvk_wav" mkdir dvk_wav
python src\dvk_tci_gui.py
if errorlevel 1 ( echo. & echo [FOUT] Zie melding. & pause )
