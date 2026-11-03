# -*- coding: utf-8 -*-
"""VB-CABLE 装好/重启后的自检：播放端能否打开 → 环回是否真的有信号。

用法: python vbcable_selftest.py
"""
import array
import ctypes
import struct
import sys
import threading
import time
import math
from ctypes import wintypes

import wb_app as W

RATE = 16000
ERR = {0: "OK", 2: "BADDEVICEID", 4: "ALLOCATED", 5: "INVALHANDLE",
       11: "INVALPARAM", 32: "WAVERR_BADFORMAT", 33: "WAVERR_STILLPLAYING"}


def probe_out(dev, rate, ch=2):
    h = ctypes.c_void_p()
    f = W._WAVEFORMATEX(wFormatTag=1, nChannels=ch, nSamplesPerSec=rate,
                        nAvgBytesPerSec=rate * ch * 2, nBlockAlign=ch * 2,
                        wBitsPerSample=16, cbSize=0)
    rc = W._winmm.waveOutOpen(ctypes.byref(h), ctypes.c_void_p(dev), ctypes.byref(f),
                              None, None, 0)
    if rc == 0:
        W._winmm.waveOutClose(h)
    return rc


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


def find_in_device():
    caps = WAVEINCAPS()
    for i in range(W._winmm.waveInGetNumDevs()):
        W._winmm.waveInGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps))
        if "cable output" in (caps.szPname or "").lower():
            return i, caps.szPname
    return -1, ""


def record(dev, seconds, out):
    W._winmm.waveInOpen.argtypes = (ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                    ctypes.POINTER(W._WAVEFORMATEX), ctypes.c_void_p,
                                    ctypes.c_void_p, ctypes.c_uint)
    fmt = W._WAVEFORMATEX(wFormatTag=1, nChannels=2, nSamplesPerSec=RATE,
                          nAvgBytesPerSec=RATE * 4, nBlockAlign=4,
                          wBitsPerSample=16, cbSize=0)
    h = ctypes.c_void_p()
    if W._winmm.waveInOpen(ctypes.byref(h), ctypes.c_void_p(dev), ctypes.byref(fmt),
                           None, None, 0) != 0:
        print("  waveInOpen 失败")
        return
    period = RATE // 10
    bufs, hdrs = [], []
    for _ in range(6):
        b = ctypes.create_string_buffer(period * 4)
        hdr = WAVEHDR()
        hdr.lpData = ctypes.cast(b, ctypes.c_void_p)
        hdr.dwBufferLength = period * 4
        bufs.append(b)
        hdrs.append(hdr)
        W._winmm.waveInPrepareHeader(h, ctypes.byref(hdr), ctypes.sizeof(hdr))
        W._winmm.waveInAddBuffer(h, ctypes.byref(hdr), ctypes.sizeof(hdr))
    W._winmm.waveInStart(h)
    t0 = time.time()
    while time.time() - t0 < seconds:
        for b, hdr in zip(bufs, hdrs):
            if hdr.dwFlags & 0x10:
                n = hdr.dwBytesRecorded
                if n:
                    out.frombytes(b.raw[:n])
                W._winmm.waveInUnprepareHeader(h, ctypes.byref(hdr), ctypes.sizeof(hdr))
                hdr.dwFlags = 0
                hdr.dwBytesRecorded = 0
                W._winmm.waveInPrepareHeader(h, ctypes.byref(hdr), ctypes.sizeof(hdr))
                W._winmm.waveInAddBuffer(h, ctypes.byref(hdr), ctypes.sizeof(hdr))
        time.sleep(0.01)
    W._winmm.waveInStop(h)
    W._winmm.waveInReset(h)
    for hdr in hdrs:
        W._winmm.waveInUnprepareHeader(h, ctypes.byref(hdr), ctypes.sizeof(hdr))
    W._winmm.waveInClose(h)


def main():
    print("=== 1) 播放端 ===")
    dev, name = W.find_cable_device()
    print(f"  find_cable_device -> dev={dev} name={name!r}")
    if dev < 0:
        print("  [x] 没找到 CABLE 播放端")
        return 1
    rates_ok = []
    for rate in (RATE, 44100, 48000):
        rc = probe_out(dev, rate)
        print(f"  {rate}Hz/2ch -> {ERR.get(rc, rc)}")
        if rc == 0:
            rates_ok.append(rate)
    if not rates_ok:
        print("  [x] 所有采样率都打不开 —— 设备还在等重启")
        return 1

    print("=== 2) 环回 ===")
    in_dev, in_name = find_in_device()
    print(f"  录音端 dev={in_dev} name={in_name!r}")
    if in_dev < 0:
        print("  [x] 没找到 CABLE Output")
        return 1

    rec = array.array("h")
    th = threading.Thread(target=record, args=(in_dev, 3.0, rec), daemon=True)
    th.start()
    time.sleep(0.5)

    w = W._CableWriter()
    opened = w.open()
    print(f"  _CableWriter.open() -> {opened} {w.stats()}")
    if not opened:
        return 1
    tone = b"".join(struct.pack("<h", int(10000 * math.sin(2 * math.pi * 1000 * i / RATE)))
                    for i in range(RATE // 10))
    for _ in range(15):
        w.write(tone)
        time.sleep(0.1)
    time.sleep(0.4)
    w.close()
    th.join(timeout=3)

    peak = max((abs(v) for v in rec), default=0)
    rms = (sum(v * v for v in rec) / len(rec)) ** 0.5 if rec else 0
    print(f"  录回 {len(rec)} 样本 ({len(rec) / 2 / RATE:.2f}s)  峰值={peak}  RMS={rms:.0f}")
    print("  结论:", "环回通 ✓" if peak > 2000 else "环回无信号 ✗")
    return 0 if peak > 2000 else 1


if __name__ == "__main__":
    sys.exit(main())
