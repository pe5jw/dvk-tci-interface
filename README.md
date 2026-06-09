# DVK-TCI Interface
---
Beware this stil need debugging its not prefect (yet :) )

---

**N1MM+ ↔ TCI Server bridge with DVK voice keyer**
*PE5JW / 2026*

[![CI](https://github.com/pe5jw/dvk-tci-interface/actions/workflows/ci.yml/badge.svg)](https://github.com/pe5jw/dvk-tci-interface/actions)

Connects N1MM+ to a TCI-compatible SDR server (Zeus, Thetis, ExpertSDR) via a Kenwood TS-2000 CAT emulation over TCP. DVK audio is streamed directly over the TCI protocol — no virtual audio cable required.

---

## Features

- **CAT emulation** — N1MM+ connects as Kenwood TS-2000 on TCP port 4532
- **Frequency & mode** — bidirectional sync between N1MM+ and TCI VFO
- **PTT** — N1MM+ CAT TX/RX command → TCI `trx:0,true,tci;`
- **DVK voice keyer** — N1MM+ F-keys trigger WAV playback via TCI TX+RX audio stream
- **GUI** — status panel, frequency display, DVK memory leds, log window
- **Record & playback** — record WAV files from microphone, play back locally or via TCI
- **Auto-reconnect** — reconnects to TCI server automatically on disconnect
- **Console mode** — headless operation via `start.bat`

---

## Requirements

- Python 3.10 or newer
- `pip install websockets sounddevice numpy`
  *(start scripts install these automatically)*
- TCI server: Zeus, Thetis, ExpertSDR2 or ExpertSDR3

---

## Quick Start

1. Unzip to a folder (e.g. `C:\ham\dvk-tci-interface\`)
2. Edit `config.ini` — set `tci_url` to your TCI server address
3. Add your WAV recordings to `dvk_wav\` as `mem1.wav` … `mem8.wav`
4. Start TCI server (Zeus / Thetis / ExpertSDR)
5. Double-click `start_gui.bat` (GUI) or `start.bat` (console)
6. Start N1MM+

**Start order:** TCI server → DVK-TCI Interface → N1MM+

---

## Configuration (`config.ini`)

```ini
[bridge]
# TCP port for N1MM+ (Kenwood TS-2000)
cat_port = 4532

# TCI server address
# Zeus (OpenHPSDR) : ws://192.168.x.x:40001
# Thetis           : ws://localhost:50001
# ExpertSDR3       : ws://localhost:50001
# ExpertSDR2       : ws://localhost:40001
tci_url = ws://192.168.x.x:40001

# TCI receiver index (0 = first)
tci_receiver = 0

# Folder with DVK WAV files
dvk_dir = dvk_wav

[audio]
# DVK playback volume to TCI TX stream (0.0 - 2.0, 1.0 = normal)
dvk_tx_volume = 1.0

# TCI audio sample rate
sample_rate = 48000

# Samples per TCI audio frame (2400 = 50ms at 48kHz)
frame_samples = 2400

[logging]
# DEBUG, INFO, WARNING, ERROR
level = INFO
```

---

## N1MM+ Setup

### Step 1 — Link radio

`Config` → `Configure Ports, Mode Control, Audio, Other` → **Hardware** tab

| Field | Value |
|-------|-------|
| Radio Nr | 1 |
| Port | **Network** |
| Radio | **Kenwood TS-2000** |
| Network address | **127.0.0.1** |
| Port Nr | **4532** |
| ✅ PTT via Radio Command | checked |

### Step 2 — DVK function keys

`Config` → `Change SSB Function Key Definitions` → add to the `.mc` file:

```
[Run]
F1={CAT1ASC FH01;}
F2={CAT1ASC FH02;}
F3={CAT1ASC FH03;}
F4={CAT1ASC FH04;}
F5={CAT1ASC FH05;}
F6={CAT1ASC FH06;}
F7={CAT1ASC FH07;}
F8={CAT1ASC FH08;}

[S&P]
F1={CAT1ASC FH02;}
F2={CAT1ASC FH03;}
```

---

## DVK WAV Files

Place recordings in `dvk_wav\` named `mem1.wav` through `mem8.wav`.  
Format: mono or stereo WAV, 44100 or 48000 Hz, 16-bit.

**Using the GUI:** click ⏺ REC next to any memory slot, speak, click ⏹ Stop.  
▶ PLAY previews locally. **TCI** button streams via TCI to the radio.

**Generating sample files** (requires espeak-ng):
```
python make_dvk_samples.py PE5JW
```

---

## TCI Protocol Notes

These findings were established by testing against Zeus (ExpertSDR3 2.0 protocol):

### PTT
```
trx:0,true,tci;    ← ,tci suffix routes TCI audio to TX chain — REQUIRED
trx:0,false,tci;
```
Without `,tci` the PTT works but TCI audio is not routed to TX.

### Audio init sequence (order is critical)
```
audio_start:0;
tx_stream_audio_buffering:50;
audio_stream_samples:2048;
audio_stream_channels:2;
audio_stream_sample_type:float32;
audio_samplerate:48000;    ← LAST — triggers streaming
```
Do **not** send `tx_enable` — it conflicts with TX audio routing.

### TX audio binary frame header (64 bytes)
```
[0]  uint32 LE  receiver    = 0
[4]  uint32 LE  sample_rate = 48000
[8]  uint32 LE  format      = 3  (FLOAT32)
[12] uint32 LE  codec       = 0
[16] uint32 LE  crc         = 0
[20] uint32 LE  length      = number of float32 values (stereo: samples × 2)
[24] uint32 LE  type        = 2  (TX_AUDIO_STREAM)
[28] uint32 LE  channels    = 2  (stereo)
[32-63]         reserved    = 0  (32 bytes)
```
Payload: float32 LE stereo interleaved (L, R, L, R, …)

### RX audio binary frame header (8 bytes)
```
[0]  uint16 LE  type        = 0 or 1
[2]  uint16 LE  receiver    = 0
[4]  uint32 LE  sample_rate = 48000
```

---

## Utility: TCI Tone Test

Tests whether TX audio reaches the TCI server correctly.

```
cd dvk-tci-interface
venv\Scripts\activate
python tci_tone_test.py ws://192.168.x.x:40001
```

Sends a 1 kHz test tone for 2 seconds. Check the Zeus spectrum for a signal.

---

## Compatibility

| Software | TCI URL | Protocol |
|---|---|---|
| Zeus (OpenHPSDR) | `ws://IP:40001` | ExpertSDR3 2.0 |
| Thetis (OpenHPSDR) | `ws://localhost:50001` | ExpertSDR3 2.0 |
| ExpertSDR3 | `ws://localhost:50001` | ExpertSDR3 2.0 |
| ExpertSDR2 | `ws://localhost:40001` | TCI 1.x |

---

## Troubleshooting

**N1MM+ shows "no radio"**  
Check port 4532 in N1MM+ Hardware tab. Windows Firewall may block TCP 4532.

**PTT works but no audio**  
Check that `trx:0,true,tci;` is sent (set `level = DEBUG`).  
Check that `mem1.wav` … `mem8.wav` exist in `dvk_wav\`.

**TCI connection refused**  
Check `tci_url` — IP address must include dots (`192.168.8.141`, not `192.168.8141`).  
The bridge auto-retries every 5 seconds.

**GUI won't start (sounddevice error)**  
Run: `venv\Scripts\activate` then `pip install sounddevice numpy`  
Or use `start.bat` for console-only mode (no mic/playback needed).

---

## Project Structure

```
dvk-tci-interface/
├── src/
│   ├── dvk_tci_interface.py   # headless bridge (CAT + TCI + DVK)
│   └── dvk_tci_gui.py         # GUI wrapper
├── tests/
│   └── test_cat.py            # unit tests (pytest)
├── dvk_wav/                   # place mem1.wav … mem8.wav here
├── config.ini                 # configuration
├── requirements.txt
├── start.bat                  # start console version (Windows)
├── start_gui.bat              # start GUI version (Windows)
├── start.sh                   # start console version (Linux/macOS)
├── tci_tone_test.py           # TCI audio diagnostic tool
└── make_dvk_samples.py        # generate sample WAV files
```

---

## License

GNU General Public License v2 — see LICENSE

73 de PE5JW
