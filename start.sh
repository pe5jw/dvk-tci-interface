#!/bin/bash
cd "$(dirname "$0")"

# Controleer Python
if ! command -v python3 &>/dev/null; then
    echo "[FOUT] Python3 niet gevonden."
    exit 1
fi

# Venv aanmaken indien nodig
if [ ! -d "venv" ]; then
    echo "[INFO] Virtuele omgeving aanmaken..."
    python3 -m venv venv
    source venv/bin/activate
    pip install -q websockets
else
    source venv/bin/activate
fi

mkdir -p dvk_wav

echo ""
echo "=== DVK-TCI Interface — N1MM+ <-> TCI Server ==="
echo "Configuratie: config.ini"
echo "Stoppen: Ctrl+C"
echo ""

python3 src/dvk_tci_interface.py
