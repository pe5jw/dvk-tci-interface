"""
DVK-TCI Interface v1.0 — GUI
PE5JW / 2026
"""

import asyncio
import configparser
import logging
import math
import queue
import re
import struct
import sys
import threading
import wave
from pathlib import Path
from tkinter import *
from tkinter import ttk, messagebox

try:
    import sounddevice as sd
    import numpy as np
    HAS_AUDIO = True
except ImportError:
    HAS_AUDIO = False

import websockets

# ── Kleuren ──────────────────────────────────────────────────────────────────
C = {
    "bg":       "#0f1117",
    "panel":    "#1a1d27",
    "border":   "#2a2d3a",
    "text":     "#c8d0e0",
    "dim":      "#5a6070",
    "accent":   "#4a9eff",
    "green":    "#3ddc84",
    "amber":    "#ffb347",
    "red":      "#ff4f4f",
    "log_info": "#7dd3fc",
    "log_warn": "#fbbf24",
    "log_err":  "#f87171",
    "log_dvk":  "#a78bfa",
    "log_cat":  "#34d399",
}
FM  = ("Consolas", 9)
FMS = ("Consolas", 8)
FML = ("Consolas", 18, "bold")
FL  = ("Segoe UI", 8)

CONFIG_FILE = Path(__file__).parent.parent / "config.ini"
DEFAULT_CONFIG = """
[bridge]
cat_port = 4532
tci_url = ws://localhost:40001
tci_receiver = 0
dvk_dir = dvk_wav

[audio]
dvk_tx_volume = 1.0
sample_rate = 48000
frame_samples = 2400

[logging]
level = INFO
"""

# ── Log queue ─────────────────────────────────────────────────────────────────
log_queue: queue.Queue = queue.Queue(maxsize=2000)

class QueueHandler(logging.Handler):
    def emit(self, record):
        try: log_queue.put_nowait(record)
        except queue.Full: pass

log = logging.getLogger("dvk_tci")

# ── State ─────────────────────────────────────────────────────────────────────
class State:
    def __init__(self):
        self.freq_hz = 0; self.mode = "---"; self.ptt = False
        self.tci_connected = False; self.cat_clients = 0
        self.dvk_playing = 0
        self.tci_queue = None; self.dvk_queue = None
        self.tci_audio_ready = None
        self.tci_audio_sample_rate = 48000

    def mode_to_tci(self):
        return {"USB":"USB","LSB":"LSB","CW":"CW","CWR":"CW-R","FM":"NFM",
                "AM":"AM","RTTY":"RTTY","RTTYR":"RTTY-R","DIG":"DIGL","PKT":"DIGU"
               }.get(self.mode.upper(),"USB")

    def tci_mode_to_n1mm(self, m):
        return {"USB":"USB","LSB":"LSB","CW":"CW","CW-R":"CWR","NFM":"FM","FM":"FM",
                "AM":"AM","RTTY":"RTTY","RTTY-R":"RTTYR","DIGL":"DIG","DIGU":"PKT"
               }.get(m.upper(),"USB")

state = State()

# ── Audio helpers ─────────────────────────────────────────────────────────────
def wav_to_float32_stereo(path: Path, target_rate=48000, volume=1.0) -> bytes:
    with wave.open(str(path),"rb") as wf:
        n_ch=wf.getnchannels(); sw=wf.getsampwidth()
        orig_sr=wf.getframerate(); raw=wf.readframes(wf.getnframes())
    if sw==2: samples=[s/32768.0 for s in struct.unpack(f"<{len(raw)//2}h",raw)]
    elif sw==4: samples=[s/2147483648.0 for s in struct.unpack(f"<{len(raw)//4}i",raw)]
    else: samples=[(s-128)/128.0 for s in struct.unpack(f"<{len(raw)}B",raw)]
    if n_ch==2: samples=[(samples[i]+samples[i+1])/2.0 for i in range(0,len(samples),2)]
    if orig_sr!=target_rate:
        ratio=orig_sr/target_rate; out_len=int(len(samples)/ratio); res=[]
        for i in range(out_len):
            src=i*ratio; idx=int(src); frac=src-idx
            res.append(samples[idx]*(1-frac)+samples[min(idx+1,len(samples)-1)]*frac)
        samples=res
    if volume!=1.0: samples=[min(1.0,max(-1.0,s*volume)) for s in samples]
    out=bytearray()
    for s in samples:
        b=struct.pack("<f",s); out+=b; out+=b
    return bytes(out)

def make_tci_tx_frame(pcm,receiver,sr=48000):
    n=len(pcm)//4; h=struct.pack("<8I",receiver,sr,3,0,0,n,2,2)
    return h+bytes(32)+pcm

def make_tci_rx_frame(pcm,receiver,sr=48000):
    n=len(pcm)//4; h=struct.pack("<8I",receiver,sr,3,0,0,n,0,2)
    return h+bytes(32)+pcm

# ── TCI client ────────────────────────────────────────────────────────────────
TCI_RECONNECT = 5

async def tci_client(url, receiver, sample_rate, frame_samples):
    log.info("TCI client: %s", url)
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                state.tci_connected = True
                log.info("TCI verbonden: %s", url)
                timeout = 10
                while timeout > 0:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=2)
                        if isinstance(msg,str) and "ready" in msg.lower(): break
                    except asyncio.TimeoutError: timeout -= 1
                for cmd in [
                    f"audio_start:{receiver};",
                    f"tx_stream_audio_buffering:50;",
                    f"audio_stream_samples:{frame_samples};",
                    f"audio_stream_channels:2;",
                    f"audio_stream_sample_type:float32;",
                    f"audio_samplerate:{sample_rate};",
                    f"vfo:{receiver},0;",
                    f"modulation:{receiver};",
                ]:
                    await ws.send(cmd); await asyncio.sleep(0.07)
                await asyncio.sleep(0.2)
                state.tci_audio_ready.set()
                log.info("TCI audio gereed (sr=%d frames=%d)", sample_rate, frame_samples)
                r = asyncio.create_task(_tci_recv(ws,receiver))
                s = asyncio.create_task(_tci_send(ws))
                done,pending = await asyncio.wait([r,s],return_when=asyncio.FIRST_COMPLETED)
                for t in pending: t.cancel()
                state.tci_connected = False; state.tci_audio_ready.clear()
        except (websockets.WebSocketException, OSError, ConnectionRefusedError) as e:
            state.tci_connected = False; state.tci_audio_ready.clear()
            log.warning("TCI verbroken: %s — opnieuw in %ds", e, TCI_RECONNECT)
        await asyncio.sleep(TCI_RECONNECT)

async def _tci_recv(ws, receiver):
    async for msg in ws:
        if isinstance(msg,bytes): continue
        msg=msg.strip().rstrip(";"); parts=msg.split(":",1)
        cmd=parts[0].lower(); args=parts[1].split(",") if len(parts)>1 else []
        if cmd=="vfo" and len(args)>=3:
            try:
                if int(args[0])==receiver: state.freq_hz=int(float(args[2]))
            except ValueError: pass
        elif cmd=="modulation" and len(args)>=2:
            try:
                if int(args[0])==receiver: state.mode=state.tci_mode_to_n1mm(args[1])
            except (ValueError,IndexError): pass
        elif cmd=="trx" and len(args)>=2:
            try:
                if int(args[0])==receiver: state.ptt=args[1].lower()=="true"
            except (ValueError,IndexError): pass

async def _tci_send(ws):
    while True:
        cmd = await state.tci_queue.get()
        try: await ws.send(cmd)
        except websockets.WebSocketException as e:
            log.warning("TCI send: %s",e); break

def tci_send(cmd):
    try: state.tci_queue.put_nowait(cmd)
    except asyncio.QueueFull: pass

# ── DVK handler ───────────────────────────────────────────────────────────────
async def dvk_handler(dvk_dir, receiver, tx_volume, sample_rate, frame_samples):
    frame_bytes = frame_samples*2*4
    frame_dur   = frame_samples/sample_rate
    silence     = bytes(frame_bytes)
    log.info("DVK handler gereed: %s", dvk_dir)
    while True:
        action, idx = await state.dvk_queue.get()
        if action != "play": continue
        wav_path = dvk_dir/f"mem{idx}.wav"
        if not wav_path.exists():
            log.warning("DVK niet gevonden: %s", wav_path); continue
        try:
            await asyncio.wait_for(state.tci_audio_ready.wait(), timeout=5)
        except asyncio.TimeoutError:
            log.warning("TCI niet gereed, DVK geannuleerd"); continue
        try:
            pcm = wav_to_float32_stereo(wav_path, sample_rate, tx_volume)
        except Exception as e:
            log.error("WAV laden: %s", e); continue
        dur = len(pcm)/(sample_rate*2*4)
        log.info("DVK play mem%d: %.2fs", idx, dur)
        state.dvk_playing = idx
        for _ in range(3):
            tci_send(make_tci_tx_frame(silence,receiver))
            tci_send(make_tci_rx_frame(silence,receiver))
            await asyncio.sleep(frame_dur*0.4)
        state.ptt = True
        tci_send(f"trx:{receiver},true,tci;")
        log.info("DVK PTT ON")
        await asyncio.sleep(0.05)
        total = len(pcm)//frame_bytes
        for fi in range(total):
            chunk = pcm[fi*frame_bytes:(fi+1)*frame_bytes]
            tci_send(make_tci_tx_frame(chunk,receiver))
            tci_send(make_tci_rx_frame(chunk,receiver))
            await asyncio.sleep(frame_dur*0.85)
        rest = pcm[total*frame_bytes:]
        if rest:
            rest = rest+bytes(frame_bytes-len(rest))
            tci_send(make_tci_tx_frame(rest,receiver))
            tci_send(make_tci_rx_frame(rest,receiver))
            await asyncio.sleep(frame_dur)
        for _ in range(2):
            tci_send(make_tci_tx_frame(silence,receiver))
            tci_send(make_tci_rx_frame(silence,receiver))
            await asyncio.sleep(frame_dur*0.4)
        await asyncio.sleep(0.1)
        state.ptt = False; state.dvk_playing = 0
        tci_send(f"trx:{receiver},false,tci;")
        log.info("DVK PTT OFF — klaar")

# ── CAT server ────────────────────────────────────────────────────────────────
MODE_MAP = {1:"LSB",2:"USB",3:"CW",4:"FM",5:"AM",6:"RTTY",7:"CWR",9:"RTTYR"}
MODE_REV = {v:k for k,v in MODE_MAP.items()}

def freq_to_cat(hz): return f"{hz:011d}"
def parse_cat(data):
    return [c.strip() for c in data.decode("ascii",errors="ignore").split(";") if c.strip()]

def handle_cat_command(cmd, receiver):
    cmd=cmd.upper().strip()
    if cmd=="FA": return f"FA{freq_to_cat(state.freq_hz)};".encode()
    if cmd.startswith("FA") and len(cmd)>2:
        try: state.freq_hz=int(cmd[2:]); tci_send(f"vfo:{receiver},0,{state.freq_hz};")
        except ValueError: pass
        return b""
    if cmd=="FB": return f"FB{freq_to_cat(state.freq_hz)};".encode()
    if cmd=="IF":
        mi=MODE_REV.get(state.mode.upper(),2)
        return f"IF{freq_to_cat(state.freq_hz)}     0000000{'1' if state.ptt else '0'}{mi}00000;".encode()
    if cmd=="MD": return f"MD{MODE_REV.get(state.mode.upper(),2)};".encode()
    if cmd.startswith("MD") and len(cmd)>2:
        try: state.mode=MODE_MAP.get(int(cmd[2]),"USB"); tci_send(f"modulation:{receiver},{state.mode_to_tci()};")
        except (ValueError,IndexError): pass
        return b""
    if cmd in ("TX","TX0","TX1"):
        state.ptt=True; tci_send(f"trx:{receiver},true,cat;"); return b""
    if cmd=="RX":
        state.ptt=False; tci_send(f"trx:{receiver},false,cat;"); return b""
    m=re.match(r"FH0?(\d)",cmd)
    if m:
        idx=int(m.group(1))
        log.info("DVK: FH%02d -> mem%d",idx,idx)
        state.dvk_queue.put_nowait(("play",idx)); return b""
    m=re.match(r"PB0?(\d+)",cmd)
    if m:
        state.dvk_queue.put_nowait(("play",int(m.group(1)))); return b""
    if cmd=="PS": return b"PS1;"
    if cmd=="ID": return b"ID019;"
    if cmd.startswith("AI"): return b"AI0;"
    if cmd.startswith("RM"): return b"RM10050;"
    return None

async def handle_cat_client(reader, writer, receiver):
    addr=writer.get_extra_info("peername")
    state.cat_clients+=1
    log.info("N1MM+ verbonden: %s", addr)
    buf=b""
    try:
        while True:
            chunk=await reader.read(256)
            if not chunk: break
            buf+=chunk
            while b";" in buf:
                idx=buf.index(b";"); raw=buf[:idx]; buf=buf[idx+1:]
                for c in parse_cat(raw+b";"):
                    resp=handle_cat_command(c,receiver)
                    if resp:
                        try: writer.write(resp); await writer.drain()
                        except (ConnectionAbortedError,ConnectionResetError,BrokenPipeError,OSError): return
    except (ConnectionResetError,asyncio.IncompleteReadError,ConnectionAbortedError,BrokenPipeError,OSError): pass
    finally:
        state.cat_clients-=1
        log.info("N1MM+ gesloten: %s", addr)
        try: writer.close()
        except Exception: pass

async def start_cat_server(port, receiver):
    server=await asyncio.start_server(lambda r,w:handle_cat_client(r,w,receiver),"0.0.0.0",port)
    log.info("CAT server: 0.0.0.0:%d (TS-2000)", port)
    async with server: await server.serve_forever()

# ── Async runner ──────────────────────────────────────────────────────────────
_loop = None

def start_async_loop(cfg):
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    cat_port     = cfg.getint("bridge","cat_port",fallback=4532)
    tci_url      = cfg.get("bridge","tci_url",fallback="ws://localhost:40001")
    receiver     = cfg.getint("bridge","tci_receiver",fallback=0)
    dvk_dir_name = cfg.get("bridge","dvk_dir",fallback="dvk_wav")
    tx_volume    = cfg.getfloat("audio","dvk_tx_volume",fallback=1.0)
    sample_rate  = cfg.getint("audio","sample_rate",fallback=48000)
    frame_samples = cfg.getint("audio","frame_samples",fallback=2400)
    dvk_dir = Path(__file__).parent.parent/dvk_dir_name
    dvk_dir.mkdir(exist_ok=True)

    async def _run():
        state.tci_queue       = asyncio.Queue(maxsize=512)
        state.dvk_queue       = asyncio.Queue(maxsize=8)
        state.tci_audio_ready = asyncio.Event()
        await asyncio.gather(
            start_cat_server(cat_port, receiver),
            tci_client(tci_url, receiver, sample_rate, frame_samples),
            dvk_handler(dvk_dir, receiver, tx_volume, sample_rate, frame_samples),
        )
    _loop.run_until_complete(_run())

# ── GUI ───────────────────────────────────────────────────────────────────────
class App:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("DVK-TCI Interface  v1.0  —  PE5JW")
        self.root.configure(bg=C["bg"])
        self.root.minsize(900, 600)
        self.cfg = self._load_config()
        self._recording = False
        self._rec_thread = None
        self._rec_data = []
        self._rec_idx = 0
        self._play_thread = None
        self._build()
        self._start_bridge()
        self._poll()

    def _load_config(self):
        cfg = configparser.ConfigParser()
        cfg.read_string(DEFAULT_CONFIG)
        if CONFIG_FILE.exists(): cfg.read(CONFIG_FILE)
        return cfg

    def _save_config(self):
        with open(CONFIG_FILE,"w") as f: self.cfg.write(f)

    # ── UI builder ────────────────────────────────────────────────────────────
    def _build(self):
        # Top bar
        top = Frame(self.root, bg=C["bg"], pady=6)
        top.pack(fill=X, padx=10)
        Label(top, text="DVK-TCI Interface", font=("Consolas",13,"bold"),
              fg=C["accent"], bg=C["bg"]).pack(side=LEFT)
        Label(top, text="v1.0  —  PE5JW 2026", font=FMS,
              fg=C["dim"], bg=C["bg"]).pack(side=LEFT, padx=8)
        Button(top, text="⚙ Instellingen", font=FMS,
               bg=C["panel"], fg=C["text"], relief=FLAT, padx=8, pady=3,
               activebackground=C["border"], activeforeground=C["accent"],
               command=self._open_settings).pack(side=RIGHT)
        if not HAS_AUDIO:
            Label(top, text="⚠ pip install sounddevice numpy",
                  font=FMS, fg=C["red"], bg=C["bg"]).pack(side=RIGHT, padx=8)

        Frame(self.root, height=1, bg=C["border"]).pack(fill=X)

        main = Frame(self.root, bg=C["bg"])
        main.pack(fill=BOTH, expand=True, padx=10, pady=8)
        main.columnconfigure(0, weight=0, minsize=270)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(0, weight=1)

        # ── Linker kolom ──────────────────────────────────────────────────────
        left = Frame(main, bg=C["bg"])
        left.grid(row=0, column=0, sticky="nsew", padx=(0,8))

        # Verbindingen
        conn = self._panel(left, "Verbindingen")
        conn.pack(fill=X, pady=(0,6))
        self._status_row(conn, "CAT poort",
            self.cfg.get("bridge","cat_port",fallback="4532"), "cat_dot")
        self._status_row(conn, "N1MM+ clients", "0", "n1mm_dot",
            show_dot=False, lbl_attr="lbl_n1mm")
        self._status_row(conn, "TCI server",
            self.cfg.get("bridge","tci_url",fallback="---"), "tci_dot")

        # Radio status
        radio = self._panel(left, "Radio")
        radio.pack(fill=X, pady=(0,6))
        ff = Frame(radio, bg=C["panel"])
        ff.pack(fill=X, padx=8, pady=4)
        Label(ff, text="VFO", font=FL, fg=C["dim"], bg=C["panel"]).pack(anchor=W)
        self.lbl_freq = Label(ff, text="--- . --- . ---",
            font=FML, fg=C["accent"], bg=C["panel"])
        self.lbl_freq.pack(anchor=W)
        mf = Frame(radio, bg=C["panel"])
        mf.pack(fill=X, padx=8, pady=(0,6))
        Label(mf, text="Modus", font=FL, fg=C["dim"], bg=C["panel"]).pack(side=LEFT)
        self.lbl_mode = Label(mf, text="---", font=FM, fg=C["text"], bg=C["panel"])
        self.lbl_mode.pack(side=LEFT, padx=8)
        self.lbl_ptt = Label(mf, text="RX", font=("Consolas",9,"bold"),
            fg=C["green"], bg=C["panel"])
        self.lbl_ptt.pack(side=RIGHT)

        # DVK paneel met opname/afspeel knoppen
        dvk = self._panel(left, "DVK Memories")
        dvk.pack(fill=X, pady=(0,6))

        if not HAS_AUDIO:
            Label(dvk, text="sounddevice niet beschikbaar",
                  font=FMS, fg=C["amber"], bg=C["panel"]).pack(padx=8, pady=2)

        # Header rij
        hdr = Frame(dvk, bg=C["panel"])
        hdr.pack(fill=X, padx=8, pady=(4,2))
        Label(hdr, text="     ", bg=C["panel"]).pack(side=LEFT)
        Label(hdr, text="Duur   ", font=FMS, fg=C["dim"], bg=C["panel"],
              width=7, anchor=W).pack(side=LEFT)
        Label(hdr, text="⏺ Opnemen", font=FMS, fg=C["dim"],
              bg=C["panel"], width=10).pack(side=LEFT)
        Label(hdr, text="▶ Afspelen", font=FMS, fg=C["dim"],
              bg=C["panel"], width=10).pack(side=LEFT)
        Label(hdr, text="TCI", font=FMS, fg=C["dim"],
              bg=C["panel"], width=4).pack(side=LEFT)

        self.dvk_rows = {}
        dvk_dir = Path(__file__).parent.parent / self.cfg.get("bridge","dvk_dir",fallback="dvk_wav")

        for i in range(1, 9):
            row = Frame(dvk, bg=C["panel"])
            row.pack(fill=X, padx=8, pady=1)

            # LED + label
            c = Canvas(row, width=12, height=12, bg=C["panel"], highlightthickness=0)
            c.pack(side=LEFT)
            oval = c.create_oval(1,1,11,11, fill=C["border"], outline="")
            Label(row, text=f"F{i}", font=("Consolas",8,"bold"),
                  fg=C["dim"], bg=C["panel"], width=3).pack(side=LEFT)

            # Duur label
            lbl_dur = Label(row, text="—      ", font=FMS,
                           fg=C["dim"], bg=C["panel"], width=7, anchor=W)
            lbl_dur.pack(side=LEFT)

            # Opname knop
            btn_rec = Button(row, text="⏺ REC", font=FMS, width=8,
                bg="#3d1a1a", fg=C["red"], relief=FLAT,
                activebackground="#5a2020", activeforeground=C["red"],
                command=lambda n=i: self._toggle_rec(n))
            btn_rec.pack(side=LEFT, padx=2)

            # Afspeel knop (lokaal)
            btn_play = Button(row, text="▶ PLAY", font=FMS, width=8,
                bg="#1a2d1a", fg=C["green"], relief=FLAT,
                activebackground="#2a4a2a", activeforeground=C["green"],
                command=lambda n=i: self._play_local(n))
            btn_play.pack(side=LEFT, padx=2)

            # TCI afspeel knop
            btn_tci = Button(row, text="TCI", font=FMS, width=4,
                bg=C["panel"], fg=C["accent"], relief=FLAT,
                activebackground=C["border"], activeforeground=C["accent"],
                command=lambda n=i: self._play_tci(n))
            btn_tci.pack(side=LEFT, padx=2)

            self.dvk_rows[i] = {
                "canvas": c, "oval": oval,
                "lbl_dur": lbl_dur,
                "btn_rec": btn_rec,
                "btn_play": btn_play,
                "btn_tci": btn_tci,
            }

        # Stop opname knop
        self.btn_stop_rec = Button(dvk, text="⏹ Stop opname", font=FMS,
            bg=C["panel"], fg=C["dim"], relief=FLAT, padx=8, pady=3,
            state=DISABLED, command=self._stop_rec)
        self.btn_stop_rec.pack(padx=8, pady=(2,6), anchor=W)

        self._refresh_dvk_durations()

        # ── Log venster ───────────────────────────────────────────────────────
        log_frame = Frame(main, bg=C["panel"],
                         highlightbackground=C["border"], highlightthickness=1)
        log_frame.grid(row=0, column=1, sticky="nsew")

        lh = Frame(log_frame, bg=C["border"], pady=4)
        lh.pack(fill=X)
        Label(lh, text="  Log", font=("Consolas",9,"bold"),
              fg=C["text"], bg=C["border"]).pack(side=LEFT)
        Button(lh, text="Wis", font=FMS, bg=C["border"], fg=C["dim"],
               relief=FLAT, padx=6, activebackground=C["panel"],
               command=self._clear_log).pack(side=RIGHT, padx=4)

        self.log_text = Text(log_frame, bg=C["bg"], fg=C["text"],
                            font=FMS, relief=FLAT, wrap=NONE,
                            state=DISABLED, cursor="arrow")
        sy = Scrollbar(log_frame, orient=VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=sy.set)
        sy.pack(side=RIGHT, fill=Y)
        self.log_text.pack(fill=BOTH, expand=True, padx=2, pady=2)
        for tag,col in [("INFO",C["log_info"]),("WARNING",C["log_warn"]),
                        ("ERROR",C["log_err"]),("DVK",C["log_dvk"]),
                        ("CAT",C["log_cat"]),("DIM",C["dim"])]:
            self.log_text.tag_config(tag, foreground=col)

        # Status bar
        Frame(self.root, height=1, bg=C["border"]).pack(fill=X)
        bar = Frame(self.root, bg=C["panel"], pady=3)
        bar.pack(fill=X)
        self.lbl_status = Label(bar, text="Opstarten...", font=FMS,
                                fg=C["dim"], bg=C["panel"])
        self.lbl_status.pack(side=LEFT, padx=8)
        Label(bar, text="github.com/pe5jw/dvk-tci-interface",
              font=FMS, fg=C["dim"], bg=C["panel"]).pack(side=RIGHT, padx=8)

    def _panel(self, parent, title):
        f = Frame(parent, bg=C["panel"],
                 highlightbackground=C["border"], highlightthickness=1)
        Label(f, text=f"  {title}", font=("Consolas",8,"bold"),
              fg=C["dim"], bg=C["border"], pady=3).pack(fill=X)
        return f

    def _status_row(self, parent, label, value, dot_attr,
                    show_dot=True, lbl_attr=None):
        row = Frame(parent, bg=C["panel"])
        row.pack(fill=X, padx=8, pady=2)
        Label(row, text=label, font=FL, fg=C["dim"], bg=C["panel"],
              width=13, anchor=W).pack(side=LEFT)
        if show_dot:
            c = Canvas(row, width=10, height=10, bg=C["panel"], highlightthickness=0)
            c.pack(side=LEFT, padx=4)
            oval = c.create_oval(1,1,9,9, fill=C["red"], outline="")
            setattr(self, dot_attr, (c, oval))
        lbl = Label(row, text=value, font=FMS, fg=C["text"], bg=C["panel"])
        lbl.pack(side=LEFT)
        if lbl_attr: setattr(self, lbl_attr, lbl)

    # ── DVK opname ────────────────────────────────────────────────────────────
    def _dvk_dir(self):
        d = Path(__file__).parent.parent / self.cfg.get("bridge","dvk_dir",fallback="dvk_wav")
        d.mkdir(exist_ok=True)
        return d

    def _toggle_rec(self, idx):
        if self._recording:
            if self._rec_idx == idx:
                self._stop_rec()
            return
        if not HAS_AUDIO:
            messagebox.showerror("Fout","pip install sounddevice numpy"); return
        self._rec_idx   = idx
        self._rec_data  = []
        self._recording = True
        self.btn_stop_rec.config(state=NORMAL, fg=C["red"])
        self.dvk_rows[idx]["btn_rec"].config(text="⏹ STOP", bg="#5a2020")
        log.info("Opname gestart: mem%d.wav", idx)

        sr = self.cfg.getint("audio","sample_rate",fallback=48000)

        def _rec():
            try:
                with sd.InputStream(samplerate=sr, channels=1, dtype="float32") as stream:
                    while self._recording:
                        data, _ = stream.read(1024)
                        self._rec_data.append(data.copy())
            except Exception as e:
                self.root.after(0, lambda: log.error("Opname fout: %s", e))

        self._rec_thread = threading.Thread(target=_rec, daemon=True)
        self._rec_thread.start()

    def _stop_rec(self):
        if not self._recording: return
        self._recording = False
        idx = self._rec_idx
        self.btn_stop_rec.config(state=DISABLED, fg=C["dim"])
        self.dvk_rows[idx]["btn_rec"].config(text="⏺ REC", bg="#3d1a1a")

        if self._rec_thread:
            self._rec_thread.join(timeout=2)

        if not self._rec_data:
            log.warning("Opname leeg"); return

        sr = self.cfg.getint("audio","sample_rate",fallback=48000)
        try:
            samples = np.concatenate(self._rec_data, axis=0).flatten()
            path = self._dvk_dir() / f"mem{idx}.wav"
            with wave.open(str(path),"wb") as wf:
                wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(sr)
                wf.writeframes((samples*32767).astype("<i2").tobytes())
            dur = len(samples)/sr
            log.info("Opgeslagen: mem%d.wav (%.1fs)", idx, dur)
        except Exception as e:
            log.error("Opslaan mislukt: %s", e)

        self._refresh_dvk_durations()

    # ── DVK afspelen lokaal ───────────────────────────────────────────────────
    def _play_local(self, idx):
        if not HAS_AUDIO:
            messagebox.showerror("Fout","pip install sounddevice numpy"); return
        path = self._dvk_dir() / f"mem{idx}.wav"
        if not path.exists():
            log.warning("mem%d.wav niet gevonden", idx); return

        def _play():
            try:
                with wave.open(str(path),"rb") as wf:
                    sr=wf.getframerate(); nch=wf.getnchannels()
                    sw=wf.getsampwidth(); raw=wf.readframes(wf.getnframes())
                pcm = struct.unpack(f"<{len(raw)//sw}h",raw)
                arr = np.array(pcm,dtype="float32")/32768.0
                arr = arr.reshape(-1,nch)
                log.info("Lokaal afspelen: mem%d.wav", idx)
                sd.play(arr, samplerate=sr)
                sd.wait()
            except Exception as e:
                self.root.after(0, lambda: log.error("Afspelen fout: %s", e))

        self._play_thread = threading.Thread(target=_play, daemon=True)
        self._play_thread.start()

    # ── DVK afspelen via TCI ──────────────────────────────────────────────────
    def _play_tci(self, idx):
        if state.dvk_queue is None:
            log.warning("Bridge nog niet gestart"); return
        try:
            state.dvk_queue.put_nowait(("play", idx))
            log.info("DVK TCI play: mem%d", idx)
        except Exception:
            log.warning("DVK queue vol")

    # ── Duur labels verversen ─────────────────────────────────────────────────
    def _refresh_dvk_durations(self):
        dvk_dir = self._dvk_dir()
        for i, widgets in self.dvk_rows.items():
            p = dvk_dir / f"mem{i}.wav"
            if p.exists():
                try:
                    with wave.open(str(p),"rb") as wf:
                        dur = wf.getnframes()/wf.getframerate()
                    widgets["lbl_dur"].config(text=f"{dur:.1f}s  ", fg=C["green"])
                except Exception:
                    widgets["lbl_dur"].config(text="?      ", fg=C["amber"])
            else:
                widgets["lbl_dur"].config(text="—      ", fg=C["dim"])

    # ── Log ───────────────────────────────────────────────────────────────────
    def _append_log(self, record):
        import time
        msg = record.getMessage(); lvl = record.levelname
        t   = time.localtime(record.created)
        ts  = f"{t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d}"
        if "DVK" in msg or "PTT" in msg or "mem" in msg.lower(): tag = "DVK"
        elif "N1MM" in msg or "FH" in msg or "CAT" in msg.lower(): tag = "CAT"
        elif lvl == "WARNING": tag = "WARNING"
        elif lvl in ("ERROR","CRITICAL"): tag = "ERROR"
        else: tag = "INFO"
        self.log_text.configure(state=NORMAL)
        self.log_text.insert(END, f"{ts} ", "DIM")
        self.log_text.insert(END, f"{lvl:<7} ", tag)
        self.log_text.insert(END, f"{msg}\n", tag)
        self.log_text.see(END)
        self.log_text.configure(state=DISABLED)
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 500:
            self.log_text.configure(state=NORMAL)
            self.log_text.delete("1.0","100.0")
            self.log_text.configure(state=DISABLED)

    def _clear_log(self):
        self.log_text.configure(state=NORMAL)
        self.log_text.delete("1.0",END)
        self.log_text.configure(state=DISABLED)

    # ── Poll ──────────────────────────────────────────────────────────────────
    def _poll(self):
        try:
            while True: self._append_log(log_queue.get_nowait())
        except queue.Empty: pass
        self._update_status()
        self.root.after(150, self._poll)

    def _update_status(self):
        # Dots
        if hasattr(self,"tci_dot"):
            self.tci_dot[0].itemconfig(self.tci_dot[1],
                fill=C["green"] if state.tci_connected else C["red"])
        if hasattr(self,"cat_dot"):
            self.cat_dot[0].itemconfig(self.cat_dot[1], fill=C["green"])
        if hasattr(self,"lbl_n1mm"):
            self.lbl_n1mm.config(
                text=f"{state.cat_clients} verbonden",
                fg=C["green"] if state.cat_clients>0 else C["dim"])
        # Frequentie
        if state.freq_hz > 0:
            hz=state.freq_hz
            self.lbl_freq.config(text=f"{hz//1_000_000:3d}.{(hz%1_000_000)//1000:03d}.{hz%1000:03d}")
        else:
            self.lbl_freq.config(text="--- . --- . ---")
        self.lbl_mode.config(text=state.mode)
        self.lbl_ptt.config(text="TX" if state.ptt else "RX",
                           fg=C["red"] if state.ptt else C["green"])
        # DVK leds
        dvk_dir = self._dvk_dir()
        for i, w in self.dvk_rows.items():
            if state.dvk_playing == i:
                w["canvas"].itemconfig(w["oval"], fill=C["red"])
            elif (dvk_dir/f"mem{i}.wav").exists():
                w["canvas"].itemconfig(w["oval"], fill=C["amber"])
            else:
                w["canvas"].itemconfig(w["oval"], fill=C["border"])
        # Opname highlight
        if self._recording:
            for i, w in self.dvk_rows.items():
                if i == self._rec_idx:
                    w["canvas"].itemconfig(w["oval"], fill=C["red"])
        # Status bar
        if self._recording:
            self.lbl_status.config(text=f"⏺ Opnemen mem{self._rec_idx}...", fg=C["red"])
        elif not state.tci_connected:
            self.lbl_status.config(text=f"Verbinden: {self.cfg.get('bridge','tci_url',fallback='?')}...", fg=C["amber"])
        elif state.ptt:
            self.lbl_status.config(text="TX — uitzendan", fg=C["red"])
        elif state.dvk_playing:
            self.lbl_status.config(text=f"DVK TCI: mem{state.dvk_playing}", fg=C["log_dvk"])
        else:
            self.lbl_status.config(text="Gereed", fg=C["green"])

    # ── Instellingen ──────────────────────────────────────────────────────────
    def _open_settings(self):
        win = Toplevel(self.root); win.title("Instellingen")
        win.configure(bg=C["bg"]); win.resizable(False,False); win.grab_set()

        f = Frame(win, bg=C["bg"], padx=12, pady=12); f.pack()

        def row(label, section, key, fallback, r, width=28):
            Label(f, text=label, font=FL, fg=C["dim"], bg=C["bg"],
                  width=18, anchor=W).grid(row=r,column=0,sticky=W,pady=3)
            e = Entry(f, font=FMS, bg=C["panel"], fg=C["text"],
                     insertbackground=C["text"], relief=FLAT,
                     highlightbackground=C["border"], highlightthickness=1, width=width)
            e.insert(0, self.cfg.get(section,key,fallback=fallback))
            e.grid(row=r,column=1,padx=8,pady=3)
            return e

        Label(f,text="Bridge",font=("Consolas",9,"bold"),
              fg=C["accent"],bg=C["bg"]).grid(row=0,column=0,columnspan=2,sticky=W)
        e_cat = row("CAT TCP poort","bridge","cat_port","4532",1,10)
        e_tci = row("TCI server URL","bridge","tci_url","ws://localhost:40001",2)
        e_rx  = row("TCI receiver","bridge","tci_receiver","0",3,4)
        e_dvk = row("DVK map","bridge","dvk_dir","dvk_wav",4)

        Frame(f,height=1,bg=C["border"]).grid(row=5,column=0,columnspan=2,sticky=EW,pady=6)
        Label(f,text="Audio",font=("Consolas",9,"bold"),
              fg=C["accent"],bg=C["bg"]).grid(row=6,column=0,columnspan=2,sticky=W)
        e_vol = row("TX volume (0.0-2.0)","audio","dvk_tx_volume","1.0",7,8)
        e_sr  = row("Sample rate","audio","sample_rate","48000",8,8)
        e_fs  = row("Frame samples","audio","frame_samples","2400",9,8)

        Frame(f,height=1,bg=C["border"]).grid(row=10,column=0,columnspan=2,sticky=EW,pady=6)
        Label(f,text="Logging",font=("Consolas",9,"bold"),
              fg=C["accent"],bg=C["bg"]).grid(row=11,column=0,columnspan=2,sticky=W)
        lvl_var = StringVar(value=self.cfg.get("logging","level",fallback="INFO"))
        ttk.Combobox(f, textvariable=lvl_var,
                    values=["DEBUG","INFO","WARNING","ERROR"],
                    state="readonly",width=10,font=FMS
                    ).grid(row=12,column=1,padx=8,pady=3,sticky=W)

        def save():
            self.cfg.set("bridge","cat_port",      e_cat.get().strip())
            self.cfg.set("bridge","tci_url",        e_tci.get().strip())
            self.cfg.set("bridge","tci_receiver",   e_rx.get().strip())
            self.cfg.set("bridge","dvk_dir",        e_dvk.get().strip())
            self.cfg.set("audio","dvk_tx_volume",   e_vol.get().strip())
            self.cfg.set("audio","sample_rate",     e_sr.get().strip())
            self.cfg.set("audio","frame_samples",   e_fs.get().strip())
            self.cfg.set("logging","level",         lvl_var.get())
            self._save_config()
            logging.getLogger().setLevel(getattr(logging,lvl_var.get(),logging.INFO))
            self._refresh_dvk_durations()
            log.info("Instellingen opgeslagen — herstart voor bridge wijzigingen")
            win.destroy()

        br = Frame(f,bg=C["bg"]); br.grid(row=13,column=0,columnspan=2,pady=8)
        Button(br,text="Opslaan",font=FMS,bg=C["accent"],fg="#000",
               relief=FLAT,padx=16,pady=4,command=save).pack(side=LEFT,padx=4)
        Button(br,text="Annuleren",font=FMS,bg=C["panel"],fg=C["text"],
               relief=FLAT,padx=12,pady=4,command=win.destroy).pack(side=LEFT,padx=4)

    # ── Start bridge ──────────────────────────────────────────────────────────
    def _start_bridge(self):
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)
        for h in root_logger.handlers[:]: root_logger.removeHandler(h)
        gh = QueueHandler(); root_logger.addHandler(gh)
        ch = logging.StreamHandler(); root_logger.addHandler(ch)
        t = threading.Thread(target=start_async_loop, args=(self.cfg,), daemon=True)
        t.start()
        log.info("DVK-TCI Interface v1.0 gestart")
        if not HAS_AUDIO:
            log.warning("sounddevice niet beschikbaar — installeer met: pip install sounddevice numpy")


def main():
    root = Tk()
    App(root)
    root.mainloop()

if __name__ == "__main__":
    main()
