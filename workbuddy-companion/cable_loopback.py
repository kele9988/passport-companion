"""VB-CABLE 环回自检：写 CABLE Input，同时从 CABLE Output 录回。

用途：区分「声卡内部环回坏了」和「输入法没在听」——前者是驱动问题，
后者是微信输入法的麦克风/快捷键设置问题。两者现象都是取字 0 个。
"""
import array
import ctypes
import math
import struct
import threading
import time
from ctypes import wintypes

import wb_app as W

winmm = W._winmm
RATE = 16000


class WAVEINCAPS(ctypes.Structure):
    _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD),
                ("vDriverVersion", wintypes.UINT), ("szPname", wintypes.WCHAR * 32),
                ("dwFormats", wintypes.DWORD), ("wChannels", wintypes.WORD),
                ("wReserved1", wintypes.WORD)]


class WAVEHDR(ctypes.Structure):
    _fields_ = [("lpData", ctypes.c_void_p), ("dwBufferLength", wintypes.DWORD),
                ("dwBytesRecorded", wintypes.DWORD), ("dwUser", ctypes.c_void_p),
                ("dwFlags", wintypes.DWORD), ("dwLoops", wintypes.DWORD),
                ("lpNext", ctypes.c_void_p), ("reserved", ctypes.c_void_p)]


winmm.waveInOpen.argtypes = (ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                             ctypes.POINTER(W._WAVEFORMATEX), ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_uint)

# ---- 1. 枚举录音端 ----
print("== 录音端 (waveIn) ==")
in_dev, in_name = -1, ""
caps = WAVEINCAPS()
for i in range(winmm.waveInGetNumDevs()):
    if winmm.waveInGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) != 0:
        continue
    nm = caps.szPname or ""
    mark = ""
    if "cable output" in nm.lower():
        in_dev, in_name = i, nm
        mark = "  <== CABLE Output"
    print(f"  [{i}] {nm!r} ch={caps.wChannels}{mark}")
if in_dev < 0:
    raise SystemExit("没有 CABLE Output 录音端，驱动异常")

# ---- 2. 枚举播放端 ----
out_dev, out_name = W.find_cable_device()
print(f"== 播放端: [{out_dev}] {out_name!r}")

# ---- 3. 边写边录 ----
rec = array.array("h")
stop = threading.Event()


def recorder():
    h = ctypes.c_void_p()
    fmt = W._WAVEFORMATEX(wFormatTag=1, nChannels=2, nSamplesPerSec=RATE,
                          nAvgBytesPerSec=RATE * 4, nBlockAlign=4,
                          wBitsPerSample=16, cbSize=0)
    rc = winmm.waveInOpen(ctypes.byref(h), ctypes.c_void_p(in_dev),
                          ctypes.byref(fmt), None, None, 0)
    if rc != 0:
        print(f"  录音端打开失败 rc={rc}")
        return
    period = RATE // 10
    bufs, hdrs = [], []
    for _ in range(8):
        b = ctypes.create_string_buffer(period * 4)
        hdr = WAVEHDR()
        hdr.lpData = ctypes.cast(b, ctypes.c_void_p)
        hdr.dwBufferLength = period * 4
        bufs.append(b)
        hdrs.append(hdr)
        winmm.waveInPrepareHeader(h, ctypes.byref(hdr), ctypes.sizeof(hdr))
        winmm.waveInAddBuffer(h, ctypes.byref(hdr), ctypes.sizeof(hdr))
    winmm.waveInStart(h)
    t0 = time.time()
    while time.time() - t0 < 4.0 and not stop.is_set():
        for b, hdr in zip(bufs, hdrs):
            if hdr.dwFlags & 0x10:  # WHDR_DONE
                n = hdr.dwBytesRecorded
                if n:
                    rec.frombytes(b.raw[:n])
                winmm.waveInUnprepareHeader(h, ctypes.byref(hdr), ctypes.sizeof(hdr))
                hdr.dwFlags = 0
                hdr.dwBytesRecorded = 0
                winmm.waveInPrepareHeader(h, ctypes.byref(hdr), ctypes.sizeof(hdr))
                winmm.waveInAddBuffer(h, ctypes.byref(hdr), ctypes.sizeof(hdr))
        time.sleep(0.01)
    winmm.waveInStop(h)
    winmm.waveInReset(h)
    for hdr in hdrs:
        winmm.waveInUnprepareHeader(h, ctypes.byref(hdr), ctypes.sizeof(hdr))
    winmm.waveInClose(h)


th = threading.Thread(target=recorder, daemon=True)
th.start()
time.sleep(0.5)

w = W._CableWriter()
opened = w.open()
print(f"== 播放端打开: {opened}  rate={getattr(w, '_rate', '?')}  stats={w.stats()}")

tone = b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * 1000 * i / RATE)))
                for i in range(RATE // 10))
n_chunks = 20
for _ in range(n_chunks):
    w.write(tone)
    time.sleep(0.1)
time.sleep(0.5)
w.close()
stop.set()
th.join(timeout=3)

peak = max((abs(v) for v in rec), default=0)
rms = (sum(v * v for v in rec) / len(rec)) ** 0.5 if rec else 0
print(f"== 录回 {len(rec)} 样本 ({len(rec) / 2 / RATE:.2f}s)  峰值={peak}  RMS={rms:.0f}")
if peak > 2000:
    print("结论：声卡环回正常 —— 问题在微信输入法（麦克风选择/快捷键/前台焦点）")
elif peak > 200:
    print("结论：环回有信号但很弱 —— 检查播放端音量与输入法增益")
else:
    print("结论：环回无信号 —— VB-CABLE 播放到录音的通路没建立，需重装或重启")
