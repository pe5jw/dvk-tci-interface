"""
TCI Tone Test v2 — PE5JW 2026
Probeert meerdere frame formaten en logt Zeus respons.
Gebruik: python tci_tone_test.py ws://IP:POORT
"""

import asyncio, math, struct, sys
import websockets

SAMPLE_RATE   = 48000
TONE_HZ       = 1000
TONE_AMP      = 0.5
TONE_SEC      = 1.5
FRAME_SAMPLES = 2048
RECEIVER      = 0


def tone_pcm(n_samples):
    """Mono float32 toon samples."""
    return [TONE_AMP * math.sin(2 * math.pi * TONE_HZ * i / SAMPLE_RATE)
            for i in range(n_samples)]


def pack_stereo_f32(samples):
    """Mono samples → stereo float32 LE bytes."""
    buf = bytearray()
    for s in samples:
        b = struct.pack("<f", max(-1.0, min(1.0, s)))
        buf += b; buf += b
    return bytes(buf)


# ── Frame formaten om te proberen ──────────────────────────────────────────

def frame_4byte(pcm: bytes, rx: int) -> bytes:
    """Oud formaat: 4-byte header (wat we eerder probeerden)."""
    return struct.pack("<BBBB", 0x02, 0x00, rx, 0x00) + pcm


def frame_64byte(pcm: bytes, rx: int, sr: int = 48000) -> bytes:
    """TCI 2.0 spec: 64-byte header."""
    n = len(pcm) // 4
    h = struct.pack("<8I", rx, sr, 3, 0, 0, n, 2, 2)
    return h + bytes(32) + pcm


def frame_raw(pcm: bytes) -> bytes:
    """Geen header — puur PCM, zoals sommige implementaties verwachten."""
    return pcm


def frame_8byte(pcm: bytes, rx: int) -> bytes:
    """8-byte header variant: type+rx+sr+len."""
    n = len(pcm) // 4
    return struct.pack("<BBHI", 0x02, rx, SAMPLE_RATE, n) + pcm


FORMATS = [
    ("64-byte TCI2.0 header",  frame_64byte),
    ("4-byte header",          frame_4byte),
    ("8-byte header",          frame_8byte),
    ("raw PCM geen header",    frame_raw),
]


async def try_format(ws, name, frame_fn, samples_pcm):
    frame_dur = FRAME_SAMPLES / SAMPLE_RATE
    n_frames = int(TONE_SEC * SAMPLE_RATE / FRAME_SAMPLES)
    silence = pack_stereo_f32([0.0] * FRAME_SAMPLES)

    print(f"\n{'─'*50}")
    print(f"  Formaat: {name}")
    print(f"{'─'*50}")

    # Pre-roll
    for _ in range(3):
        try:
            f = frame_fn(silence, RECEIVER) if frame_fn != frame_raw else frame_raw(silence)
        except TypeError:
            f = frame_fn(silence)
        await ws.send(f)
        await asyncio.sleep(frame_dur * 0.5)

    # PTT aan
    await ws.send(f"trx:{RECEIVER},true,tci;")
    print(f"  PTT ON")
    await asyncio.sleep(0.05)

    # Toon frames
    for i in range(n_frames):
        chunk_samples = tone_pcm(FRAME_SAMPLES)
        # Fase continueren
        offset = i * FRAME_SAMPLES
        chunk_samples = [TONE_AMP * math.sin(2 * math.pi * TONE_HZ * (offset + j) / SAMPLE_RATE)
                         for j in range(FRAME_SAMPLES)]
        pcm = pack_stereo_f32(chunk_samples)
        try:
            f = frame_fn(pcm, RECEIVER) if frame_fn != frame_raw else frame_raw(pcm)
        except TypeError:
            f = frame_fn(pcm)
        await ws.send(f)
        if i == 0:
            print(f"  Frame 1: {len(f)} bytes, header={f[:8].hex()}")
        await asyncio.sleep(frame_dur * 0.85)

    # Post-roll
    for _ in range(2):
        try:
            f = frame_fn(silence, RECEIVER) if frame_fn != frame_raw else frame_raw(silence)
        except TypeError:
            f = frame_fn(silence)
        await ws.send(f)
        await asyncio.sleep(frame_dur * 0.5)

    await asyncio.sleep(0.1)
    await ws.send(f"trx:{RECEIVER},false,tci;")
    print(f"  PTT OFF")

    print(f"  → Zie je nu een signaal in Zeus? (j/n): ", end="", flush=True)
    await asyncio.sleep(2.5)   # wacht even zodat gebruiker kan kijken


async def run(url: str):
    print(f"Verbinden: {url}\n")

    async with websockets.connect(url, ping_interval=None) as ws:
        # Wacht op ready, log alles
        print("TCI handshake:")
        timeout = 8
        while timeout > 0:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=2)
                if isinstance(msg, str):
                    line = msg.strip()
                    print(f"  ← {line}")
                    if "ready" in line.lower():
                        break
                timeout -= 1
            except asyncio.TimeoutError:
                timeout -= 2

        # Audio stream opzetten
        print("\nAudio streams initialiseren...")
        cmds = [
            f"audio_start:{RECEIVER};",
            f"tx_stream_audio_buffering:50;",
            f"audio_stream_samples:{FRAME_SAMPLES};",
            f"audio_stream_channels:2;",
            f"audio_stream_sample_type:float32;",
            f"audio_samplerate:{SAMPLE_RATE};",
                    ]
        for cmd in cmds:
            await ws.send(cmd)
            print(f"  → {cmd.strip()}")
            await asyncio.sleep(0.07)

        await asyncio.sleep(0.5)

        # Log eventuele Zeus responses
        responses = []
        try:
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=0.3)
                if isinstance(msg, str):
                    responses.append(msg.strip())
        except asyncio.TimeoutError:
            pass
        if responses:
            print("\nZeus responses na init:")
            for r in responses:
                print(f"  ← {r}")

        # Probeer elk formaat
        print(f"\n{'═'*50}")
        print(f"  START FORMAAT TESTS")
        print(f"  Let op het Zeus spectrum tijdens elke test")
        print(f"{'═'*50}")

        for name, fn in FORMATS:
            await try_format(ws, name, fn, None)
            await asyncio.sleep(1.0)   # pauze tussen formaten

        print(f"\n{'═'*50}")
        print("Klaar. Welk formaat gaf een signaal in Zeus?")
        print("Meld dat terug zodat we de correcte header kunnen vaststellen.")
        print(f"{'═'*50}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Gebruik: python tci_tone_test.py ws://IP:POORT")
        sys.exit(1)
    try:
        asyncio.run(run(sys.argv[1]))
    except KeyboardInterrupt:
        print("\nAfgebroken.")
    except Exception as e:
        print(f"\nFout: {e}")
        raise
