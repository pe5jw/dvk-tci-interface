# DVK-TCI Interface — N1MM+ ↔ TCI Server
**PE5JW / 2026**

Lichte brug-app die N1MM+ verbindt met een TCI-server (Zeus, Thetis, ExpertSDR2/3)
via Kenwood TS-2000 CAT-emulatie over TCP. DVK audio wordt direct via de TCI TX+RX
audio stream verstuurd — geen VB-Cable of andere virtual audio software nodig.

---

## Architectuur

```
N1MM+ (logger)
    |
    | TCP 4532 — Kenwood TS-2000 CAT
    |
[ DVK-TCI Interface ]
    |
    | WebSocket — TCI protocol (ExpertSDR3 2.0)
    |
TCI Server (Zeus / Thetis / ExpertSDR2/3)
    |
    | Protocol1 / HPSDR
    |
Radio (HL2, ANAN, etc.)
```

---

## Functies

- **Frequentie lezen/schrijven** — N1MM+ bandmap ↔ TCI VFO
- **Modus** — N1MM+ mode ↔ TCI modulation
- **PTT** — N1MM+ CAT TX/RX ↔ TCI `trx:0,true/false,tci;`
- **DVK afspelen** — N1MM+ F-toetsen → WAV → float32 PCM → TCI TX+RX stream
- **Automatische herverbinding** — als TCI server herstart

---

## Vereisten

- Python 3.10 of nieuwer
- `pip install websockets` (start scripts doen dit automatisch)
- Windows: geen extra drivers nodig
- TCI server: Zeus, Thetis, ExpertSDR2 of ExpertSDR3

---

## Installatie

1. Pak de zip uit (bijv. `C:\ham\dvk-tci-interface\`)
2. Dubbelklik `start.bat`
3. Eerste keer: venv wordt aangemaakt en websockets geïnstalleerd

---

## Configuratie (`config.ini`)

```ini
[bridge]
cat_port = 4532          # TCP poort voor N1MM+ (Kenwood TS-2000)

# TCI server adres:
# Zeus       : ws://IP:40001
# Thetis     : ws://localhost:50001
# ExpertSDR3 : ws://localhost:50001
# ExpertSDR2 : ws://localhost:40001
tci_url = ws://192.168.x.x:40001

tci_receiver = 0         # TCI receiver index (0 = eerste)
dvk_dir = dvk_wav        # map met WAV bestanden

[audio]
dvk_tx_volume = 1.0      # volume TX stream (0.0 - 2.0)
sample_rate = 48000      # TCI audio sample rate
frame_samples = 2400     # samples per TCI frame (50ms bij 48kHz)

[logging]
level = INFO             # DEBUG voor meer detail
```

---

## N1MM+ instellen

### Stap 1 — Radio koppelen

`Config` → `Configure Ports, Mode Control, Audio, Other` → tabblad **Hardware**

| Veld | Waarde |
|------|--------|
| Radio Nr | 1 |
| Port | **Network** |
| Radio | **Kenwood TS-2000** |
| Network address | **127.0.0.1** |
| Port Nr | **4532** |
| ✅ PTT via Radio Command | aanvinken |

### Stap 2 — DVK functietoetsen

`Config` → `Change SSB Function Key Definitions` → voeg toe aan het `.mc` bestand:

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

## DVK WAV bestanden

Zet opnames in `dvk_wav\` als `mem1.wav` t/m `mem8.wav`.
Formaat: mono of stereo WAV, 44100 of 48000 Hz, 16-bit.

Gebruik de **DVK Recorder** app (`dvk-recorder`) om opnames te maken.

---

## Opstartsvolgorde

1. Start de **TCI server** (Zeus / Thetis / ExpertSDR)
2. Start **DVK-TCI Interface** (`start.bat`)
3. Start **N1MM+**

---

## TCI audio protocol — bevindingen

Na uitgebreid testen met Zeus (ExpertSDR3 2.0 protocol) zijn de volgende
bevindingen gedocumenteerd voor toekomstige ontwikkelaars:

### PTT via TCI
```
trx:0,true,tci;     ← ,tci suffix is VERPLICHT om TCI audio naar TX te routen
trx:0,false,tci;
```
Zonder de `,tci` suffix accepteert Zeus de PTT wel maar wordt de TCI audio
stream NIET naar de TX chain gerouteerd.

### TX audio init volgorde (KRITISCH)
```
audio_start:0;
tx_stream_audio_buffering:50;
audio_stream_samples:2048;
audio_stream_channels:2;
audio_stream_sample_type:float32;
audio_samplerate:48000;        ← LAATSTE — triggert streaming
```
`audio_samplerate` als laatste sturen is verplicht. `tx_enable` NIET sturen —
dat verstoort de TX audio routing.

### TX audio binary frame header (64 bytes)
```
[0]  uint32 LE  receiver    = 0
[4]  uint32 LE  sample_rate = 48000
[8]  uint32 LE  format      = 3 (FLOAT32)
[12] uint32 LE  codec       = 0
[16] uint32 LE  crc         = 0
[20] uint32 LE  length      = aantal float32 waarden (stereo: samples * 2)
[24] uint32 LE  type        = 2 (TX_AUDIO_STREAM)
[28] uint32 LE  channels    = 2 (stereo)
[32-63]         reserved    = 0 (32 bytes)
```
Payload: float32 LE stereo interleaved (L,R,L,R,...)

### RX audio binary frame header (8 bytes)
```
[0]  uint16 LE  type        = 0 of 1
[2]  uint16 LE  receiver    = 0
[4]  uint32 LE  sample_rate = 48000
```
Payload: float32 LE stereo interleaved

---

## Hulptools

### `tci_tone_test.py` — TCI verbinding testen
Test of TX audio correct aankomt bij de TCI server.
```
cd dvk-tci-interface
venv\Scripts\activate
python tci_tone_test.py ws://192.168.x.x:40001
```
Stuurt een 1kHz testtoon van 2 seconden via TCI TX stream.
Controleer in het spectrum of het signaal zichtbaar is.

---

## Compatibiliteit

| Software | TCI URL | Protocol |
|---|---|---|
| Zeus (OpenHPSDR) | `ws://IP:40001` | ExpertSDR3 2.0 |
| Thetis (OpenHPSDR) | `ws://localhost:50001` | ExpertSDR3 2.0 |
| ExpertSDR3 | `ws://localhost:50001` | ExpertSDR3 2.0 |
| ExpertSDR2 | `ws://localhost:40001` | TCI 1.x |

---

## Probleemoplossing

**N1MM+ zegt "no radio":**
Controleer poort 4532 in N1MM+ Hardware tab. Firewall kan TCP 4532 blokkeren.

**PTT werkt maar geen audio:**
Controleer of `trx:0,true,tci;` verstuurd wordt (zie log met `level = DEBUG`).
Controleer of `mem1.wav` t/m `mem8.wav` bestaan in `dvk_wav\`.

**TCI verbinding mislukt:**
Controleer `tci_url` in config.ini. Zorg dat het IP-adres punten heeft
(bijv. `192.168.8.141`, niet `192.168.8141`).

**Geen audio maar PTT werkt wel:**
Zet `level = DEBUG` in config.ini en kijk of `DVK play mem1:` verschijnt.
Controleer of de WAV-bestanden niet stil zijn (amplitude > 0).

---

73 de PE5JW
