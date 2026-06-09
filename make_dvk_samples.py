#!/usr/bin/env python3
"""
Maak sample DVK WAV bestanden aan voor mem1.wav t/m mem8.wav
Gebruikt espeak-ng (als beschikbaar) of schrijft stille WAV placeholders.

Gebruik: python make_dvk_samples.py PE5JW
"""

import os
import struct
import subprocess
import sys
import wave
from pathlib import Path

CALLSIGN = sys.argv[1] if len(sys.argv) > 1 else "PE5JW"
DVK_DIR = Path(__file__).parent / "dvk_wav"
DVK_DIR.mkdir(exist_ok=True)

MESSAGES = [
    f"CQ CQ CQ Contest, this is {CALLSIGN}, CQ Contest",
    CALLSIGN,
    "5 9",
    "5 9 Thank you 73",
    f"Please copy {CALLSIGN}",
    "Your report is 5 9, QSL?",
    f"QRZ? {CALLSIGN}",
    "73, good luck in the contest",
]


def make_silence_wav(path: Path, duration_sec: float = 1.0, sample_rate: int = 44100):
    """Write a silent WAV file as placeholder."""
    n = int(duration_sec * sample_rate)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * n)


def try_espeak(text: str, path: Path) -> bool:
    for cmd in (
        ["espeak-ng", "-v", "en-gb", "-s", "150", "-w", str(path), text],
        ["espeak",    "-v", "en",    "-s", "150", "-w", str(path), text],
    ):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=15)
            if result.returncode == 0 and path.exists():
                return True
        except FileNotFoundError:
            continue
    return False


for i, msg in enumerate(MESSAGES, start=1):
    out = DVK_DIR / f"mem{i}.wav"
    if out.exists():
        print(f"  mem{i}.wav bestaat al, overgeslagen")
        continue
    print(f"  mem{i}.wav : {msg}")
    if not try_espeak(msg, out):
        make_silence_wav(out)
        print(f"           -> stille placeholder (espeak niet gevonden)")
    else:
        print(f"           -> OK")

print(f"\nDVK bestanden staan in: {DVK_DIR}")
print("Vervang mem1.wav t/m mem8.wav door je eigen opnames.")
