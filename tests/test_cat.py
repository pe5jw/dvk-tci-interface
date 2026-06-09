"""Unit tests DVK-TCI Interface — python -m pytest tests/ -v"""
import asyncio, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import dvk_tci_interface as b

def setup_function():
    b.state.freq_hz = 14_074_000
    b.state.mode = "USB"
    b.state.ptt = False
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    b.state.tci_queue = asyncio.Queue(maxsize=512)
    b.state.dvk_queue = asyncio.Queue(maxsize=8)
    b.state.tci_audio_ready = asyncio.Event()

def test_fa_read():       assert b.handle_cat_command("FA",0) == b"FA00014074000;"
def test_fa_write():
    b.handle_cat_command("FA00007074000",0)
    assert b.state.freq_hz == 7_074_000
def test_fb_mirrors_fa():
    b.state.freq_hz = 14_200_000
    assert b.handle_cat_command("FB",0) == b"FB00014200000;"
def test_md_usb():        assert b.handle_cat_command("MD",0) == b"MD2;"
def test_md_lsb():
    b.state.mode="LSB"; assert b.handle_cat_command("MD",0) == b"MD1;"
def test_md_write_cw():
    b.handle_cat_command("MD3",0); assert b.state.mode == "CW"
def test_tx_ptt():
    b.handle_cat_command("TX",0); assert b.state.ptt is True
def test_rx_ptt():
    b.state.ptt=True; b.handle_cat_command("RX",0); assert b.state.ptt is False
def test_if_freq():       assert b"14074000" in b.handle_cat_command("IF",0)
def test_id():            assert b.handle_cat_command("ID",0) == b"ID019;"
def test_ps():            assert b.handle_cat_command("PS",0) == b"PS1;"
def test_unknown():       assert b.handle_cat_command("ZZ",0) is None

def test_fh_play():
    b.handle_cat_command("FH01",0)
    action, idx = b.state.dvk_queue.get_nowait()
    assert action == "play" and idx == 1

def test_fh8_play():
    b.handle_cat_command("FH08",0)
    action, idx = b.state.dvk_queue.get_nowait()
    assert action == "play" and idx == 8

def test_pb_play():
    b.handle_cat_command("PB03",0)
    action, idx = b.state.dvk_queue.get_nowait()
    assert action == "play" and idx == 3

def test_tx_frame_header():
    pcm = bytes(8)  # 2 float32 stereo samples
    frame = b.make_tci_tx_frame(pcm, 0)
    import struct
    # Header: 8x uint32 LE + 32 reserved = 64 bytes
    assert len(frame) == 64 + len(pcm)
    vals = struct.unpack("<8I", frame[:32])
    assert vals[0] == 0       # receiver
    assert vals[1] == 48000   # sample_rate
    assert vals[2] == 3       # FLOAT32
    assert vals[6] == 2       # TX type

def test_rx_frame_header():
    pcm = bytes(8)
    frame = b.make_tci_rx_frame(pcm, 0)
    import struct
    assert len(frame) == 64 + len(pcm)
    vals = struct.unpack("<8I", frame[:32])
    assert vals[6] == 0       # RX type

def test_freq_to_cat():
    assert b.freq_to_cat(14_074_000) == "00014074000"
    assert b.freq_to_cat(1_840_000)  == "00001840000"

def test_mode_to_tci():
    b.state.mode = "USB"; assert b.state.mode_to_tci() == "USB"
    b.state.mode = "CW";  assert b.state.mode_to_tci() == "CW"
