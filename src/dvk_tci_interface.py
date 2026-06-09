"""
DVK-TCI Interface — N1MM+ <-> TCI Server
PE5JW / 2026

- TCP CAT server (Kenwood TS-2000) voor N1MM+
- TCI WebSocket client (Zeus, Thetis, ExpertSDR2/3)
- DVK afspelen: WAV -> resample -> float32 PCM -> TCI TX stream
  (audio gaat naar zowel TX als RX stream zodat andere apps het ook ontvangen)
"""

import asyncio
import configparser
import logging
import re
import struct
import sys
import wave
from pathlib import Path

import websockets

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)-14s %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
log = logging.getLogger("dvk_tci")

CONFIG_FILE = Path(__file__).parent.parent / "config.ini"

DEFAULT_CONFIG = """
[bridge]
cat_port = 4532
tci_url = ws://localhost:40001
tci_receiver = 0
dvk_dir = dvk_wav

[audio]
# Volume DVK afspelen naar TCI TX stream (0.0 - 2.0, 1.0 = normaal)
dvk_tx_volume = 1.0
# TCI audio sample rate
sample_rate = 48000
# Samples per TCI frame (2400 = 50ms bij 48kHz)
frame_samples = 2400

[logging]
level = INFO
"""

# TCI binary audio frame header (4 bytes):
# [0] stream type: 0x00 = RX audio, 0x02 = TX audio
# [1] 0x00
# [2] receiver/trx index
# [3] 0x00 reserved
# Payload: float32 LE stereo interleaved
TCI_RX_AUDIO_MAGIC = 0x00
TCI_TX_AUDIO_MAGIC = 0x02


def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_string(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        cfg.read(CONFIG_FILE)
    else:
        CONFIG_FILE.write_text(DEFAULT_CONFIG.strip())
        log.info("Config aangemaakt: %s", CONFIG_FILE)
    lvl = cfg.get("logging", "level", fallback="INFO").upper()
    logging.getLogger().setLevel(getattr(logging, lvl, logging.INFO))
    return cfg


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
class State:
    def __init__(self):
        self.freq_hz: int = 14_074_000
        self.mode: str = "USB"
        self.ptt: bool = False
        self.tci_queue: asyncio.Queue = None
        self.dvk_queue: asyncio.Queue = None
        self.tci_audio_ready: asyncio.Event = None
        self.tci_audio_sample_rate: int = 48000

    def mode_to_tci(self) -> str:
        MAP = {
            "USB": "USB", "LSB": "LSB", "CW": "CW", "CWR": "CW-R",
            "FM": "NFM", "AM": "AM", "RTTY": "RTTY", "RTTYR": "RTTY-R",
            "DIG": "DIGL", "PKT": "DIGU",
        }
        return MAP.get(self.mode.upper(), "USB")

    def tci_mode_to_n1mm(self, tci_mode: str) -> str:
        MAP = {
            "USB": "USB", "LSB": "LSB", "CW": "CW", "CW-R": "CWR",
            "NFM": "FM", "FM": "FM", "AM": "AM", "RTTY": "RTTY",
            "RTTY-R": "RTTYR", "DIGL": "DIG", "DIGU": "PKT",
        }
        return MAP.get(tci_mode.upper(), "USB")


state = State()


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def wav_to_float32_stereo(path: Path, target_rate: int = 48000,
                           volume: float = 1.0) -> bytes:
    """Lees WAV, resample naar target_rate, lever float32 stereo LE bytes."""
    with wave.open(str(path), "rb") as wf:
        n_ch    = wf.getnchannels()
        sw      = wf.getsampwidth()
        orig_sr = wf.getframerate()
        n_fr    = wf.getnframes()
        raw     = wf.readframes(n_fr)

    # Decode naar float
    if sw == 2:
        samples = [s / 32768.0 for s in struct.unpack(f"<{len(raw)//2}h", raw)]
    elif sw == 4:
        samples = [s / 2147483648.0 for s in struct.unpack(f"<{len(raw)//4}i", raw)]
    else:
        samples = [(s - 128) / 128.0 for s in struct.unpack(f"<{len(raw)}B", raw)]

    # Stereo -> mono
    if n_ch == 2:
        samples = [(samples[i] + samples[i+1]) / 2.0 for i in range(0, len(samples), 2)]

    # Resample
    if orig_sr != target_rate:
        ratio   = orig_sr / target_rate
        out_len = int(len(samples) / ratio)
        res     = []
        for i in range(out_len):
            src = i * ratio
            idx = int(src)
            frac = src - idx
            if idx + 1 < len(samples):
                res.append(samples[idx] * (1.0 - frac) + samples[idx+1] * frac)
            elif idx < len(samples):
                res.append(samples[idx])
        samples = res

    # Volume clip
    if volume != 1.0:
        samples = [min(1.0, max(-1.0, s * volume)) for s in samples]

    # Stereo float32 LE
    out = bytearray()
    for s in samples:
        b = struct.pack("<f", s)
        out += b  # L
        out += b  # R (mono duplicated)
    return bytes(out)


def make_tci_audio_frame(pcm: bytes, receiver: int, stream_type: int,
                          sample_rate: int = 48000) -> bytes:
    """
    TCI 2.0 audio binary frame — 64-byte header + float32 PCM payload.

    Header layout (8 x uint32 LE + 8 x uint32 reserved = 64 bytes):
      [0]  receiver      — trx index (0)
      [1]  sample_rate   — 48000
      [2]  format        — 3 = FLOAT32
      [3]  codec         — 0
      [4]  crc           — 0
      [5]  length        — aantal float32 samples (stereo: n_samples * 2)
      [6]  type          — 0 = RX_AUDIO, 2 = TX_AUDIO
      [7]  channels      — 2 (stereo)
      [8-15] reserved    — 0
    """
    n_floats = len(pcm) // 4  # aantal float32 waarden
    header = struct.pack("<8I",
        receiver,       # [0] receiver
        sample_rate,    # [1] sample_rate
        3,              # [2] format = FLOAT32
        0,              # [3] codec
        0,              # [4] crc
        n_floats,       # [5] length
        stream_type,    # [6] type: 0=RX, 2=TX
        2,              # [7] channels = stereo
    )
    # 8 reserved uint32 = 32 bytes, total header = 64 bytes
    header += bytes(32)
    return header + pcm


def make_tci_tx_frame(pcm: bytes, receiver: int, sample_rate: int = 48000) -> bytes:
    return make_tci_audio_frame(pcm, receiver, 2, sample_rate)  # type 2 = TX

def make_tci_rx_frame(pcm: bytes, receiver: int, sample_rate: int = 48000) -> bytes:
    return make_tci_audio_frame(pcm, receiver, 0, sample_rate)  # type 0 = RX


# ---------------------------------------------------------------------------
# TCI WebSocket client
# ---------------------------------------------------------------------------
TCI_RECONNECT_DELAY = 5


async def tci_client(url: str, receiver: int, sample_rate: int, frame_samples: int):
    log.info("TCI client gestart: %s (RX %d)", url, receiver)
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                log.info("TCI verbonden: %s", url)

                # Wacht op 'ready'
                timeout = 10
                while timeout > 0:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=2)
                        if isinstance(msg, str) and "ready" in msg.lower():
                            break
                    except asyncio.TimeoutError:
                        timeout -= 1

                # Status opvragen
                await ws.send(f"vfo:{receiver},0;")
                await ws.send(f"modulation:{receiver};")

                # Audio start volgorde — audio_start eerst, audio_samplerate LAATSTE
                # Deze volgorde is bevestigd werkend met Zeus en Thetis.
                # audio_samplerate als laatste triggert de streaming.
                audio_cmds = [
                    f"audio_start:{receiver};",
                    f"tx_stream_audio_buffering:50;",
                    f"audio_stream_samples:{frame_samples};",
                    f"audio_stream_channels:2;",
                    f"audio_stream_sample_type:float32;",
                    f"audio_samplerate:{sample_rate};",   # LAATSTE — triggert streaming
                ]
                for cmd in audio_cmds:
                    await ws.send(cmd)
                    await asyncio.sleep(0.07)   # 70ms tussen elk commando

                await asyncio.sleep(0.3)
                state.tci_audio_ready.set()
                log.info("TCI audio geconfigureerd (sr=%d, frames=%d)", sample_rate, frame_samples)

                recv_task = asyncio.create_task(_tci_recv(ws, receiver))
                send_task = asyncio.create_task(_tci_send(ws))
                done, pending = await asyncio.wait(
                    [recv_task, send_task], return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                state.tci_audio_ready.clear()

        except (websockets.WebSocketException, OSError, ConnectionRefusedError) as e:
            log.warning("TCI verbroken: %s — opnieuw in %ds", e, TCI_RECONNECT_DELAY)
            state.tci_audio_ready.clear()
        await asyncio.sleep(TCI_RECONNECT_DELAY)


async def _tci_recv(ws, receiver: int):
    async for msg in ws:
        if isinstance(msg, bytes):
            continue  # binary frames negeren
        msg = msg.strip().rstrip(";")
        parts = msg.split(":", 1)
        cmd  = parts[0].lower()
        args = parts[1].split(",") if len(parts) > 1 else []

        if cmd == "vfo" and len(args) >= 3:
            try:
                if int(args[0]) == receiver:
                    state.freq_hz = int(float(args[2]))
            except ValueError:
                pass
        elif cmd == "modulation" and len(args) >= 2:
            try:
                if int(args[0]) == receiver:
                    state.mode = state.tci_mode_to_n1mm(args[1])
            except (ValueError, IndexError):
                pass
        elif cmd == "trx" and len(args) >= 2:
            try:
                if int(args[0]) == receiver:
                    state.ptt = args[1].lower() == "true"
            except (ValueError, IndexError):
                pass
        elif cmd == "audio_samplerate" and args:
            try:
                state.tci_audio_sample_rate = int(args[0])
            except ValueError:
                pass


async def _tci_send(ws):
    while True:
        cmd = await state.tci_queue.get()
        try:
            await ws.send(cmd)
        except websockets.WebSocketException as e:
            log.warning("TCI send fout: %s", e)
            break


def tci_send(cmd):
    try:
        state.tci_queue.put_nowait(cmd)
    except asyncio.QueueFull:
        log.warning("TCI queue vol")


# ---------------------------------------------------------------------------
# DVK handler — alleen afspelen, TX én RX stream
# ---------------------------------------------------------------------------

async def dvk_handler(dvk_dir: Path, receiver: int, tx_volume: float,
                      sample_rate: int, frame_samples: int):
    log.info("DVK handler gestart, map: %s", dvk_dir)
    frame_bytes = frame_samples * 2 * 4  # stereo float32
    frame_dur   = frame_samples / sample_rate

    while True:
        action, idx = await state.dvk_queue.get()

        if action != "play":
            continue

        wav_path = dvk_dir / f"mem{idx}.wav"
        if not wav_path.exists():
            log.warning("DVK niet gevonden: %s", wav_path)
            continue

        # Wacht op TCI connectie
        try:
            await asyncio.wait_for(state.tci_audio_ready.wait(), timeout=5)
        except asyncio.TimeoutError:
            log.warning("TCI niet gereed, DVK geannuleerd")
            continue

        # Laad WAV
        try:
            pcm = wav_to_float32_stereo(wav_path, sample_rate, tx_volume)
        except Exception as e:
            log.error("WAV laden mislukt: %s", e)
            continue

        dur = len(pcm) / (sample_rate * 2 * 4)
        log.info("DVK play mem%d: %.2f sec, %d bytes, sr=%d", idx, dur, len(pcm), sample_rate)

        # Pre-roll stille frames (buffer vullen voor PTT)
        silence = bytes(frame_bytes)
        for _ in range(3):
            tci_send(make_tci_tx_frame(silence, receiver))
            tci_send(make_tci_rx_frame(silence, receiver))
            await asyncio.sleep(frame_dur * 0.4)

        # PTT aan
        state.ptt = True
        tci_send(f"trx:{receiver},true,tci;")
        log.info("DVK PTT ON")
        await asyncio.sleep(0.05)

        # Stuur audio naar TX én RX stream
        total_frames = len(pcm) // frame_bytes
        for fi in range(total_frames):
            chunk = pcm[fi * frame_bytes: (fi + 1) * frame_bytes]
            tci_send(make_tci_tx_frame(chunk, receiver))
            tci_send(make_tci_rx_frame(chunk, receiver))
            await asyncio.sleep(frame_dur * 0.85)

        # Rest (onvolledig frame)
        rest = pcm[total_frames * frame_bytes:]
        if rest:
            rest = rest + bytes(frame_bytes - len(rest))
            tci_send(make_tci_tx_frame(rest, receiver))
            tci_send(make_tci_rx_frame(rest, receiver))
            await asyncio.sleep(frame_dur)

        # Post-roll
        for _ in range(2):
            tci_send(make_tci_tx_frame(silence, receiver))
            tci_send(make_tci_rx_frame(silence, receiver))
            await asyncio.sleep(frame_dur * 0.4)

        await asyncio.sleep(0.1)

        # PTT uit
        state.ptt = False
        tci_send(f"trx:{receiver},false,tci;")
        log.info("DVK PTT OFF — klaar")


# ---------------------------------------------------------------------------
# CAT server (Kenwood TS-2000)
# ---------------------------------------------------------------------------
MODE_MAP = {1: "LSB", 2: "USB", 3: "CW", 4: "FM", 5: "AM", 6: "RTTY", 7: "CWR", 9: "RTTYR"}
MODE_REV = {v: k for k, v in MODE_MAP.items()}


def freq_to_cat(hz: int) -> str:
    return f"{hz:011d}"


def parse_cat(data: bytes) -> list:
    return [c.strip() for c in data.decode("ascii", errors="ignore").split(";") if c.strip()]


def handle_cat_command(cmd: str, receiver: int):
    cmd = cmd.upper().strip()
    log.debug("CAT: %s", cmd)

    if cmd == "FA":
        return f"FA{freq_to_cat(state.freq_hz)};".encode()
    if cmd.startswith("FA") and len(cmd) > 2:
        try:
            state.freq_hz = int(cmd[2:])
            tci_send(f"vfo:{receiver},0,{state.freq_hz};")
        except ValueError:
            pass
        return b""

    if cmd == "FB":
        return f"FB{freq_to_cat(state.freq_hz)};".encode()

    if cmd == "IF":
        mode_idx = MODE_REV.get(state.mode.upper(), 2)
        ptt = "1" if state.ptt else "0"
        return f"IF{freq_to_cat(state.freq_hz)}     0000000{ptt}{mode_idx}00000;".encode()

    if cmd == "MD":
        return f"MD{MODE_REV.get(state.mode.upper(), 2)};".encode()
    if cmd.startswith("MD") and len(cmd) > 2:
        try:
            state.mode = MODE_MAP.get(int(cmd[2]), "USB")
            tci_send(f"modulation:{receiver},{state.mode_to_tci()};")
        except (ValueError, IndexError):
            pass
        return b""

    if cmd in ("TX", "TX0", "TX1"):
        state.ptt = True
        tci_send(f"trx:{receiver},true,cat;")
        return b""
    if cmd == "RX":
        state.ptt = False
        tci_send(f"trx:{receiver},false,cat;")
        return b""

    m = re.match(r"FH0?(\d)", cmd)
    if m:
        idx = int(m.group(1))
        log.info("DVK: FH%02d -> play mem%d", idx, idx)
        state.dvk_queue.put_nowait(("play", idx))
        return b""

    m = re.match(r"PB0?(\d+)", cmd)
    if m:
        idx = int(m.group(1))
        log.info("DVK: PB%02d -> play mem%d", idx, idx)
        state.dvk_queue.put_nowait(("play", idx))
        return b""

    if cmd == "PS":   return b"PS1;"
    if cmd == "ID":   return b"ID019;"
    if cmd.startswith("AI"): return b"AI0;"
    if cmd.startswith("RM"): return b"RM10050;"

    log.debug("CAT onbekend: %s", cmd)
    return None


async def handle_cat_client(reader, writer, receiver):
    addr = writer.get_extra_info("peername")
    log.info("N1MM+ verbonden: %s", addr)
    buf = b""
    try:
        while True:
            chunk = await reader.read(256)
            if not chunk:
                break
            buf += chunk
            while b";" in buf:
                idx = buf.index(b";")
                raw = buf[:idx]
                buf = buf[idx + 1:]
                for c in parse_cat(raw + b";"):
                    resp = handle_cat_command(c, receiver)
                    if resp:
                        try:
                            writer.write(resp)
                            await writer.drain()
                        except (ConnectionAbortedError, ConnectionResetError,
                                BrokenPipeError, OSError):
                            return
    except (ConnectionResetError, asyncio.IncompleteReadError,
            ConnectionAbortedError, BrokenPipeError, OSError):
        pass
    finally:
        log.info("N1MM+ gesloten: %s", addr)
        try:
            writer.close()
        except Exception:
            pass


async def start_cat_server(port: int, receiver: int):
    server = await asyncio.start_server(
        lambda r, w: handle_cat_client(r, w, receiver),
        "0.0.0.0", port,
    )
    log.info("CAT server: 0.0.0.0:%d (TS-2000)", port)
    async with server:
        await server.serve_forever()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    cfg          = load_config()
    cat_port     = cfg.getint("bridge",  "cat_port",      fallback=4532)
    tci_url      = cfg.get("bridge",    "tci_url",        fallback="ws://localhost:40001")
    receiver     = cfg.getint("bridge",  "tci_receiver",  fallback=0)
    dvk_dir_name = cfg.get("bridge",    "dvk_dir",        fallback="dvk_wav")
    tx_volume    = cfg.getfloat("audio", "dvk_tx_volume", fallback=1.0)
    sample_rate  = cfg.getint("audio",  "sample_rate",    fallback=48000)
    frame_samples = cfg.getint("audio", "frame_samples",  fallback=2400)

    dvk_dir = Path(__file__).parent.parent / dvk_dir_name
    dvk_dir.mkdir(exist_ok=True)

    state.tci_queue       = asyncio.Queue(maxsize=512)
    state.dvk_queue       = asyncio.Queue(maxsize=8)
    state.tci_audio_ready = asyncio.Event()

    log.info("================================================")
    log.info("  DVK-TCI Interface  --  PE5JW 2026")
    log.info("  N1MM+ <-> TCI Server  (TS-2000 CAT + DVK)")
    log.info("================================================")
    log.info("CAT poort  : %d", cat_port)
    log.info("TCI server : %s (RX %d)", tci_url, receiver)
    log.info("DVK map    : %s", dvk_dir)
    log.info("TX volume  : %.2f", tx_volume)
    log.info("")
    log.info("N1MM+ CAT commando's:")
    log.info("  FH01-FH08  : DVK afspelen (TX+RX stream naar TCI)")
    log.info("  PB01-PB08  : DVK afspelen (Yaesu stijl)")

    await asyncio.gather(
        start_cat_server(cat_port, receiver),
        tci_client(tci_url, receiver, sample_rate, frame_samples),
        dvk_handler(dvk_dir, receiver, tx_volume, sample_rate, frame_samples),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Gestopt.")
