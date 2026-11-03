#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Passport 对讲机 · 网页版对端

让手机/电脑浏览器变成第二台对讲机：按住屏幕说话，声音从 ESP32 设备播出来；
设备说话时，浏览器也能听到。

    python3 gateway.py --port 7802 --relay 127.0.0.1:7801

链路：
    浏览器 ──WebSocket(PCM 16k)──> gateway.py ──TCP(Opus)──> relay.py ──> 设备
                                    ↑ libopus 编解码

为什么两端都传 PCM：
    浏览器的 Web Audio 只认 PCM，裸 Opus 帧它播不了；而设备只吃 Opus。
    所以编解码全部放在这台机器上做，浏览器侧不需要任何第三方库。
    Opus 编解码走系统自带的 libopus(C API),用 ctypes 直接调,
    这样整个文件零第三方依赖,和 relay.py 一样拷过来就能跑。

安全上下文提醒：
    浏览器只允许在 HTTPS 或 localhost 下访问麦克风。局域网用
    http://<ip>:7802 打开是拿不到录音权限的，要么本机用 localhost 测，
    要么让 Lucky/Cloudflare 用 HTTPS 反代过来。
"""

import argparse
import asyncio
import base64
import ctypes
import ctypes.util
import hashlib
import json
import logging
import os
import struct
import sys
import time

log = logging.getLogger("gateway")

# ---------------------------------------------------------------- libopus

SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_MS = 60
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000          # 960
PCM_FRAME_BYTES = FRAME_SAMPLES * CHANNELS * 2          # 1920

OPUS_APPLICATION_VOIP = 2048
OPUS_APPLICATION_AUDIO = 2049
OPUS_SET_BITRATE = 4002
OPUS_SET_SIGNAL = 4024
OPUS_SIGNAL_VOICE = 3001
OPUS_SET_COMPLEXITY = 4010
OPUS_SET_DTX = 4016
OPUS_SET_INBAND_FEC = 4012


def _load_opus():
    for name in ("libopus.so.0", "libopus.so", "libopus.0.dylib", "opus.dll"):
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    found = ctypes.util.find_library("opus")
    if found:
        return ctypes.CDLL(found)
    raise RuntimeError("找不到 libopus,请先安装 (apt install libopus0)")


class OpusCodec:
    """一对 16k/mono 的 Opus 编解码器。

    每个 WebSocket 会话必须各自持有一份。以前是全局共用一个实例，
    encoder/decoder 内部都是带状态的重采样+预测器，两路音频交替喂进去
    会互相污染——单人用看不出来，第二台手机一接就两头都变调、发闷。
    """

    def __init__(self):
        lib = self.lib = _load_opus()
        err = ctypes.c_int(0)

        lib.opus_encoder_create.restype = ctypes.c_void_p
        lib.opus_encoder_create.argtypes = [ctypes.c_int, ctypes.c_int,
                                            ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        # 用 AUDIO 而不是 VOIP：
        #   VOIP 模式内部走 SILK，带宽被压到 ~8kHz，人声可懂但发闷，
        #   在 16k 采样下听起来就是"糊"。
        #   AUDIO 模式走 CELT，保留全带宽，同码率下清晰度明显更高，
        #   手机外放时差别一耳朵就能听出来。
        self.enc = lib.opus_encoder_create(SAMPLE_RATE, CHANNELS,
                                           OPUS_APPLICATION_AUDIO, ctypes.byref(err))
        if err.value != 0 or not self.enc:
            raise RuntimeError("opus_encoder_create 失败: %d" % err.value)

        lib.opus_encoder_ctl.restype = ctypes.c_int
        lib.opus_encoder_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        # 40kbps：16k 单声道下对 CELT 而言绰绰有余，留足余量。
        # 之前 32k 在连续语音上偶有"糊"，这里直接给够，60ms 帧也就 300 字节。
        lib.opus_encoder_ctl(self.enc, OPUS_SET_BITRATE, 40000)
        lib.opus_encoder_ctl(self.enc, OPUS_SET_SIGNAL, OPUS_SIGNAL_VOICE)
        lib.opus_encoder_ctl(self.enc, OPUS_SET_COMPLEXITY, 10)   # 最高质量档，C3 侧只解码不吃 CPU
        lib.opus_encoder_ctl(self.enc, OPUS_SET_DTX, 0)           # 关静音压缩：对讲机静默期也要底色噪声，否则听起来像掉线
        lib.opus_encoder_ctl(self.enc, OPUS_SET_INBAND_FEC, 1)    # 带内前向纠错，丢一帧能补回来

        lib.opus_decoder_create.restype = ctypes.c_void_p
        lib.opus_decoder_create.argtypes = [ctypes.c_int, ctypes.c_int,
                                            ctypes.POINTER(ctypes.c_int)]
        self.dec = lib.opus_decoder_create(SAMPLE_RATE, CHANNELS, ctypes.byref(err))
        if err.value != 0 or not self.dec:
            raise RuntimeError("opus_decoder_create 失败: %d" % err.value)

        lib.opus_encode.restype = ctypes.c_int
        lib.opus_encode.argtypes = [ctypes.c_void_p,
                                    ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
                                    ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int]
        lib.opus_decode.restype = ctypes.c_int
        lib.opus_decode.argtypes = [ctypes.c_void_p,
                                    ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int,
                                    ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
                                    ctypes.c_int]

    def encode(self, pcm_bytes):
        """1920 字节 PCM -> 一帧 Opus(约 100~200 字节)。"""
        n = len(pcm_bytes) // 2
        arr = (ctypes.c_int16 * n).from_buffer_copy(pcm_bytes)
        out = (ctypes.c_ubyte * 512)()
        rc = self.lib.opus_encode(self.enc, arr, n, out, 512)
        if rc < 0:
            return None
        return ctypes.string_at(out, rc)

    def decode(self, opus_bytes):
        """一帧 Opus -> PCM 字节。

        必须用 string_at 按字节读那块内存。直接 bytes(out[:n]) 会炸：
        切片拿到的是 c_int16 对象，采样值为负或大于 255，
        bytes() 抛 "bytes must be in range(0, 256)" —— 这个异常会顺着
        收包协程冒出去把整条下行链路打断，表现就是"设备在说话但网页收不到"。
        """
        arr = (ctypes.c_ubyte * len(opus_bytes)).from_buffer_copy(opus_bytes)
        out = (ctypes.c_int16 * (FRAME_SAMPLES * 6))()
        rc = self.lib.opus_decode(self.dec, arr, len(opus_bytes), out, FRAME_SAMPLES * 6, 0)
        if rc < 0:
            return None
        return ctypes.string_at(out, rc * CHANNELS * 2)


# ---------------------------------------------------------------- relay 协议

MAGIC = b"TB"
HEADER = struct.Struct(">2sBHB")

T_JOIN, T_AUDIO, T_PING = 0x01, 0x02, 0x03
T_JOIN_ACK, T_PONG, T_PEERS, T_TALK = 0x81, 0x83, 0x84, 0x85


def pack(ftype, payload=b"", seq=0):
    return HEADER.pack(MAGIC, ftype, len(payload), seq & 0xFF) + payload


class RelayPeer:
    """到 relay.py 的一条 TCP 连接，代表网页端这"一台对讲机"。"""

    def __init__(self, host, port, room, devid, codec):
        self.host, self.port, self.room = host, port, room
        self.devid = devid
        self.codec = codec
        self.reader = None
        self.writer = None
        self.seq = 0
        self.peers = 0
        self.on_audio = None       # 回调: 收到对端 Opus 帧
        self.on_event = None       # 回调: dict 事件
        self._tasks = []

    async def connect(self):
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        sock = self.writer.get_extra_info("socket")
        if sock is not None:
            try:
                import socket as _s
                sock.setsockopt(_s.IPPROTO_TCP, _s.TCP_NODELAY, 1)
            except OSError:
                pass

        room_b = self.room.encode("ascii")[:4].ljust(4, b"0")
        dev_b = self.devid.encode("ascii")[:4].ljust(4, b"W")[:4]
        self.writer.write(pack(T_JOIN, room_b + dev_b + b"\0\0\0\0"))
        await self.writer.drain()

        ftype, payload = await asyncio.wait_for(self._read_frame(), 8)
        if ftype != T_JOIN_ACK or not payload:
            raise RuntimeError("JOIN 未确认")
        if payload[0] != 0:
            reason = {1: "房间已满", 2: "协议不符", 3: "口令错误"}.get(payload[0], "未知")
            raise RuntimeError("被拒绝: %s" % reason)

        self.peers = payload[1] if len(payload) > 1 else 1
        self._tasks = [asyncio.ensure_future(self._rx_loop()),
                       asyncio.ensure_future(self._hb_loop())]
        log.info("已进入房间 %s,在线 %d 人", self.room, self.peers)
        return self.peers

    async def _read_frame(self):
        hdr = await self.reader.readexactly(HEADER.size)
        magic, ftype, length, seq = HEADER.unpack(hdr)
        if magic != MAGIC:
            raise ValueError("bad magic")
        payload = await self.reader.readexactly(length) if length else b""
        return ftype, payload

    async def _rx_loop(self):
        try:
            while True:
                ftype, payload = await self._read_frame()
                if ftype == T_AUDIO:
                    if self.on_audio:
                        self.on_audio(payload)
                elif ftype == T_PEERS and payload:
                    self.peers = payload[0]
                    if self.on_event:
                        self.on_event({"type": "peers", "n": payload[0]})
                elif ftype == T_TALK and len(payload) >= 5:
                    if self.on_event:
                        self.on_event({"type": "remote", "talking": payload[0] != 0})
        except (asyncio.IncompleteReadError, ConnectionResetError, asyncio.CancelledError):
            pass
        except Exception as exc:                      # noqa: BLE001
            # 用 warning 而不是 debug:这里静默退出过一次,表现成"设备在说话
            # 但网页一点声音都没有",排查绕了很久。收包协程死掉必须显眼。
            log.warning("收包循环退出: %s", exc)
        finally:
            if self.on_event:
                self.on_event({"type": "closed"})

    async def _hb_loop(self):
        try:
            while True:
                await asyncio.sleep(10)
                now = int(time.time() * 1000)
                self.writer.write(pack(T_PING, struct.pack(">II", now >> 32, now & 0xFFFFFFFF)))
                await self.writer.drain()
        except (ConnectionResetError, asyncio.CancelledError):
            pass

    def send_audio(self, opus_frame):
        if not self.writer:
            return False
        self.writer.write(pack(T_AUDIO, opus_frame, self.seq))
        self.seq = (self.seq + 1) & 0xFF
        return True

    async def close(self):
        for t in self._tasks:
            t.cancel()
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:                         # noqa: BLE001
                pass


# ---------------------------------------------------------------- WebSocket

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


async def ws_handshake(reader, writer, headers):
    key = None
    for line in headers:
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip()
            break
    if not key:
        writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        await writer.drain()
        return False
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    writer.write(("HTTP/1.1 101 Switching Protocols\r\n"
                  "Upgrade: websocket\r\n"
                  "Connection: Upgrade\r\n"
                  "Sec-WebSocket-Accept: %s\r\n\r\n" % accept).encode())
    await writer.drain()
    return True


async def ws_read(reader):
    """读一帧。返回 (opcode, payload) 或 None 表示关闭。"""
    b1, b2 = await reader.readexactly(2)
    opcode = b1 & 0x0F
    masked = b2 & 0x80
    ln = b2 & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", await reader.readexactly(2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", await reader.readexactly(8))[0]
    mask = await reader.readexactly(4) if masked else None
    data = await reader.readexactly(ln) if ln else b""
    if mask:
        data = bytes(c ^ mask[i & 3] for i, c in enumerate(data))
    return opcode, data


def ws_pack(opcode, payload):
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    return head + payload


# ---------------------------------------------------------------- 页面

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0d1b2a">
<title>Passport 对讲机</title>
<style>
  :root{--bg:#0d1b2a;--card:#152a3f;--line:#23415c;--fg:#e8f1f8;--dim:#7fa0b8;
        --acc:#31d0aa;--red:#ff5a5a;}
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent;}
  body{margin:0;min-height:100vh;background:var(--bg);color:var(--fg);
       font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
       display:flex;flex-direction:column;align-items:center;padding:18px 16px 32px;
       user-select:none;-webkit-user-select:none;}
  h1{font-size:17px;font-weight:600;margin:0 0 14px;letter-spacing:.5px;}
  .card{width:100%;max-width:420px;background:var(--card);border:1px solid var(--line);
        border-radius:16px;padding:14px 16px;margin-bottom:14px;}
  .row{display:flex;align-items:center;justify-content:space-between;font-size:14px;
       padding:5px 0;color:var(--dim);}
  .row b{color:var(--fg);font-weight:600;}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#556;
       margin-right:7px;vertical-align:1px;transition:background .2s;}
  .dot.on{background:var(--acc);box-shadow:0 0 8px var(--acc);}
  input{flex:1;background:#0b1725;border:1px solid var(--line);border-radius:9px;
        color:var(--fg);padding:9px 11px;font-size:15px;outline:none;text-align:center;
        letter-spacing:2px;}
  button{background:var(--acc);color:#04231c;border:0;border-radius:11px;padding:10px 18px;
         font-size:15px;font-weight:700;margin-left:10px;}
  button:disabled{opacity:.4;}
  #ptt{width:100%;max-width:420px;height:150px;border-radius:24px;border:2px solid var(--line);
       background:linear-gradient(180deg,#16293d,#101f30);color:var(--fg);
       font-size:22px;font-weight:700;letter-spacing:3px;margin:6px 0 14px;
       transition:all .12s;
       /* 手机上长按按钮会弹系统菜单(拷贝/查询/在 Safari 打开),这三行是唯一的解药:
          touch-action 关掉手势劫持,touch-callout 关掉 iOS 长按气泡,
          user-select 关掉文本选择——少了任何一条,iOS/Android 各有一半机型仍会弹。*/
       touch-action:none;-webkit-touch-callout:none;
       -webkit-user-select:none;user-select:none;
       -webkit-tap-highlight-color:transparent;}
  #ptt.active{background:linear-gradient(180deg,#1f6b57,#155545);border-color:var(--acc);
              color:#eafff8;transform:scale(.98);
              box-shadow:0 0 28px rgba(49,208,170,.35);}
  #ptt:disabled{opacity:.35;}
  .meter{width:100%;max-width:420px;height:7px;background:#0b1725;border-radius:4px;
         overflow:hidden;margin-bottom:16px;}
  .meter i{display:block;height:100%;width:0;background:var(--acc);
           transition:width .08s linear;border-radius:4px;}
  .hint{font-size:12.5px;color:var(--dim);line-height:1.65;max-width:420px;}
  .hint code{background:#0b1725;padding:1px 5px;border-radius:4px;color:var(--acc);}
</style>
</head>
<body>
<h1>Passport 对讲机</h1>

<div class="card">
  <div class="row">
    <span><span class="dot" id="dot"></span><span id="stat">未连接</span></span>
    <span>在线 <b id="peers">-</b> 人</span>
  </div>
  <div class="row" style="gap:10px;color:var(--fg);">
    <span style="color:var(--dim);font-size:13px;">信道</span>
    <input id="room" value="1240" maxlength="4" inputmode="numeric">
    <button id="conn">连接</button>
  </div>
</div>

<button id="ptt" disabled>按住 说话</button>
<div class="meter"><i id="bar"></i></div>

<div class="hint">
  手机端需要 <code>https</code> 才能拿麦克风权限；本机用 <code>localhost</code> 也可以。<br>
  设备端保持在对讲机界面、信道号一致即可。松手后声音会从设备播出来。
</div>

<script>
const $ = id => document.getElementById(id);
let ws = null, audioCtx = null, micStream = null, micNode = null;
let playing = false, sendBuf = [];
const SEND_CHUNK = 1920;          // 60ms @16k/mono/s16

// ---- 下行抖动缓冲 ----
// 以前每收到一帧就 src.start()(不给时间点 = 立刻播)。网络到达时间本来就不匀,
// 早到的帧和上一帧在时间轴上重叠被混音、迟到的帧让播放头空转——听感就是
// "一卡一卡的"。改成自己维护播放时间轴 nextAt:每帧都排在前一帧结束的那一刻,
// 只在一开始留一段缓冲吸收抖动,再把积压过久的帧跳掉防止延迟滚雪球。
const JITTER_TARGET = 0.22;       // 起播缓冲 220ms(约 3.7 帧)。140ms 经隧道仍有轻微断续
const MAX_LAG      = 0.60;        // 积压超过 600ms 直接跳到最新
let nextAt = 0;

function setStat(text, on) {
  $('stat').textContent = text;
  $('dot').classList.toggle('on', !!on);
}

function connect() {
  const room = $('room').value.trim() || '1240';
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws?room=${encodeURIComponent(room)}`);
  ws.binaryType = 'arraybuffer';
  setStat('连接中…', false);

  ws.onopen = () => {
    setStat('已连接', true);
    $('conn').textContent = '断开';
    ensureAudio();          // 连接本身是用户点击引起的,借此解锁音频上下文
    nextAt = 0;
    if (playNode) playNode.port.postMessage('reset');   // 清空播放缓冲重新攒
  };
  ws.onclose = () => {
    setStat('已断开', false);
    $('conn').textContent = '连接';
    $('ptt').disabled = true;
    $('peers').textContent = '-';
  };
  ws.onerror = () => setStat('连接失败', false);
  ws.onmessage = ev => {
    if (typeof ev.data === 'string') {
      try {
        const m = JSON.parse(ev.data);
        if (m.type === 'peers') $('peers').textContent = m.n;
        else if (m.type === 'error') setStat(m.msg, false);
      } catch (e) {}
      return;
    }
    playPcm(new Int16Array(ev.data));
  };
}

function ensureAudio() {
  if (!audioCtx) {
    try {
      // 16k 能拿到就用 16k,省一次重采样;iOS 会拒绝并退回默认(48k),
      // 那时 createBuffer 声明的 16k 会被浏览器自动重采样,音调照样对。
      audioCtx = new (window.AudioContext || window.webkitAudioContext)(
        {sampleRate: 16000, latencyHint: 'interactive'});
    } catch (e) {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
  }
  if (audioCtx.state === 'suspended') audioCtx.resume();
  return audioCtx;
}

let playNode = null;              // 播放 worklet(音频线程消费)，见 TalkiePlay
async function ensurePlayNode() {
  if (!audioCtx.audioWorklet || workletBroken) return null;
  try {
    await ensureWorklet();
    if (!playNode) {
      playNode = new AudioWorkletNode(audioCtx, 'talkie-play', {
        numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [1]});
      playNode.connect(audioCtx.destination);
    }
    return playNode;
  } catch (e) {
    workletBroken = true;
    return null;
  }
}

async function playPcm(int16) {
  const n = int16.length;
  if (!n || !ensureAudio()) return;

  // 首选：喂给音频线程的播放 worklet，主线程卡顿不再影响播放连续性
  const node = await ensurePlayNode();
  if (node) {
    const f = new Float32Array(n);
    for (let i = 0; i < n; i++) f[i] = int16[i] * (1 / 32768);
    node.port.postMessage(f, [f.buffer]);
    return;
  }

  // 兼容回退：主线程定时排帧(worklet 不可用的老浏览器)
  const buf = audioCtx.createBuffer(1, n, 16000);
  const ch = buf.getChannelData(0);
  for (let i = 0; i < n; i++) ch[i] = int16[i] * (1 / 32768);

  const src = audioCtx.createBufferSource();
  src.buffer = buf;
  src.connect(audioCtx.destination);

  const now = audioCtx.currentTime;
  if (nextAt < now + 0.015) {
    // 播放头已经空转(断流)或还没起播:按当前情况重新对齐
    nextAt = now + (nextAt === 0 ? JITTER_TARGET : 0.02);
  } else if (nextAt > now + MAX_LAG) {
    nextAt = now + JITTER_TARGET;   // 积压太深,认输跳帧,保实时
  }
  src.start(nextAt);
  nextAt += buf.duration;
}

// AudioWorklet 采集器。跑在音频渲染线程上，不占主线程 —— 这是"手机发出去
// 声音断续发虚"的根因：ScriptProcessor 的 onaudioprocess 走主线程，界面重绘、
// WebSocket 收发、甚至一条日志都可能让它晚几毫秒，采集到的是带缺口的 PCM，
// 而缺口在对端听感上就是发虚/听不清。设备端由 I2S 硬件时钟精准驱动，
// 所以设备→手机那条方向从来没这个问题。
// 用 Blob URL 注入，不额外依赖一个 js 文件。
const WORKLET_SRC = `
class TalkieCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.acc = [];            // 累积的原始浮点样本
    this.pos = 0;             // 已消费到的位置(浮点,支持重采样)
    this.step = sampleRate / 16000;
    this.on = true;
    this.port.onmessage = e => { if (e.data === 'stop') { this.on = false; this.acc.length = 0; this.pos = 0; } };
  }
  process(inputs, outputs) {
    const inp = inputs[0] && inputs[0][0];
    const out = outputs[0] && outputs[0][0];
    if (out) out.fill(0);     // 输出恒为静音，绝不把麦克风送回扬声器
    if (!inp || !this.on) return true;
    for (let i = 0; i < inp.length; i++) this.acc.push(inp[i]);
    const step = this.step;
    const need = 960 * step;  // one 60ms frame, in input samples
    while (this.pos + need <= this.acc.length - 1) {
      const pcm = new Int16Array(960);
      for (let j = 0; j < 960; j++) {
        const p = this.pos + j * step;      // 线性插值重采样到 16k
        const i0 = p | 0;
        const f = p - i0;
        let v = (this.acc[i0] * (1 - f) + this.acc[i0 + 1] * f) * 32767;
        pcm[j] = v > 32767 ? 32767 : (v < -32768 ? -32768 : v);
      }
      this.pos += need;
      this.port.postMessage(pcm.buffer, [pcm.buffer]);
    }
    const drop = Math.floor(this.pos);      // 丢掉已消费的输入，别让数组无限长
    if (drop > 0) { this.acc = this.acc.slice(drop); this.pos -= drop; }
    return true;
  }
}
registerProcessor('talkie-capture', TalkieCapture);

// 播放器也放音频线程：主线程排帧(createBuffer+定时)会被 GC/重绘/触摸卡出缺口，
// 症状与采集侧当年一样。改为主线程只往环形缓冲喂数据，worklet 按输出时钟自己
// 消费：攒够 prebuf 才起播(抖动缓冲)，断流时输出静音并重新攒，积压过深丢老保新。
class TalkiePlay extends AudioWorkletProcessor {
  constructor() {
    super();
    this.cap = 32768;              // 2s @16k
    this.buf = new Float32Array(this.cap);
    this.head = 0; this.len = 0;   // 环形缓冲：head=读起点，len=有效样本数(16k 域)
    this.pos = 0;                  // 读游标(小数,相对 head)
    this.started = false;
    this.prebuf = 3520;            // 220ms @16k，与原 JITTER_TARGET 一致
    this.maxlag = 16000;           // 积压超过 1s 丢老保新，延迟不滚雪球
    this.port.onmessage = e => {
      if (e.data === 'reset') { this.started = false; this.len = 0; this.pos = 0; return; }
      const d = e.data;
      if (this.len + d.length > this.cap) {
        const drop = this.len + d.length - this.cap;
        this.head = (this.head + drop) % this.cap; this.len -= drop;
        this.pos = Math.max(0, this.pos - drop);
      }
      for (let i = 0; i < d.length; i++) this.buf[(this.head + this.len + i) % this.cap] = d[i];
      this.len += d.length;
      if (this.len > this.maxlag) {
        const drop = this.len - this.maxlag;
        this.head = (this.head + drop) % this.cap; this.len -= drop;
        this.pos = Math.max(0, this.pos - drop);
      }
    };
  }
  process(inputs, outputs) {
    const out = outputs[0] && outputs[0][0];
    if (!out) return true;
    if (!this.started) {
      out.fill(0);
      if (this.len >= this.prebuf) { this.started = true; this.pos = 0; }
      return true;
    }
    const step = 16000 / sampleRate;   // 16k 域 PCM 上采样到 ctx 采样率(线性插值)
    for (let i = 0; i < out.length; i++) {
      if (this.pos + 1 >= this.len) {  // 断流：补静音并重新攒缓冲
        out.fill(0, i);
        this.started = false; this.pos = 0;
        return true;
      }
      const i0 = this.pos | 0, f = this.pos - i0;
      const a = this.buf[(this.head + i0) % this.cap];
      const b = this.buf[(this.head + i0 + 1) % this.cap];
      out[i] = a + (b - a) * f;
      this.pos += step;
    }
    const consumed = Math.floor(this.pos);
    if (consumed > 0) {
      this.head = (this.head + consumed) % this.cap;
      this.len -= consumed; this.pos -= consumed;
    }
    return true;
  }
}
registerProcessor('talkie-play', TalkiePlay);
`;

let workletReady = null, workletBroken = false;
function ensureWorklet() {
  if (!workletReady) {
    const url = URL.createObjectURL(new Blob([WORKLET_SRC], {type: 'application/javascript'}));
    workletReady = audioCtx.audioWorklet.addModule(url).then(() => URL.revokeObjectURL(url));
  }
  return workletReady;
}

function sendPcm(pcm) {
  let peak = 0;
  for (let i = 0; i < pcm.length; i++) { const v = pcm[i] < 0 ? -pcm[i] : pcm[i]; if (v > peak) peak = v; }
  $('bar').style.width = Math.min(100, peak / 32767 * 220) + '%';
  if (ws && ws.readyState === 1) ws.send(pcm.buffer);
}

async function startTalk() {
  if (playing || !ws || ws.readyState !== 1) return;
  playing = true;
  $('ptt').classList.add('active');
  $('ptt').textContent = '松开 结束';

  ensureAudio();
  await audioCtx.resume();

  try {
    micStream = await navigator.mediaDevices.getUserMedia({
      // 关掉 AEC 与降噪：这两个是移动端人声发闷/发虚的主要来源。
      // 对讲机是半双工的,按住 PTT 时对端本来就不出声,不需要回声消除；
      // 保留自动增益,让离麦远近不至于忽大忽小。
      audio: {echoCancellation: false, noiseSuppression: false, autoGainControl: true, channelCount: 1}
    });
  } catch (e) {
    setStat('拿不到麦克风', false);
    stopTalk();
    return;
  }
  const src = audioCtx.createMediaStreamSource(micStream);

  let useWorklet = false;
  if (audioCtx.audioWorklet && !workletBroken) {
    try {
      await ensureWorklet();
      micNode = new AudioWorkletNode(audioCtx, 'talkie-capture', {
        numberOfInputs: 1, numberOfOutputs: 1,
        outputChannelCount: [1], channelCount: 1, channelCountMode: 'explicit'
      });
      micNode.port.onmessage = e => sendPcm(new Int16Array(e.data));
      useWorklet = true;
    } catch (e) {
      workletBroken = true;    // 老浏览器或 CSP 不允许，退回下面的兼容路径
      micNode = null;
    }
  }

  if (!useWorklet) {
    // 兼容路径：ScriptProcessor 在主线程。先把麦克风按原始采样率累积，
    // 再线性插值重采样到 16k(同样缺少抗混叠低通，是兜底方案；主流走 worklet)。
    micNode = audioCtx.createScriptProcessor(512, 1, 1);
    sendBuf = [];
    const step = audioCtx.sampleRate / 16000;   // 48k→16k 时 step=3，避免直接按 960 发送导致 3 倍速
    micNode.onaudioprocess = e => {
      if (!playing) return;
      const inp = e.inputBuffer.getChannelData(0);
      for (let i = 0; i < inp.length; i++) sendBuf.push(Math.max(-1, Math.min(1, inp[i])));
      const need = Math.ceil(960 * step);
      while (sendBuf.length >= need + 1) {
        const pcm = new Int16Array(960);
        for (let j = 0; j < 960; j++) {
          const p = j * step;
          const i0 = p | 0;
          const f = p - i0;
          let v = (sendBuf[i0] * (1 - f) + sendBuf[i0 + 1] * f) * 32767;
          pcm[j] = v > 32767 ? 32767 : (v < -32768 ? -32768 : (v | 0));
        }
        sendPcm(pcm);
        sendBuf = sendBuf.slice(Math.round(960 * step));
      }
    };
  }

  // 向网关上报真实采样率与采集方式，方便对账
  try {
    ws.send(JSON.stringify({type: 'hello', sr: audioCtx.sampleRate, wl: useWorklet ? 1 : 0}));
  } catch (e) {}

  src.connect(micNode);
  // 必须接出去才会被拉取。中间垫一个 gain=0 的哑节点：链路照走，声音不出，
  // 手机贴脸时不会自己听自己。
  const mute = audioCtx.createGain();
  mute.gain.value = 0;
  micNode.channelCount = 1;
  micNode.connect(mute);
  mute.connect(audioCtx.destination);
  micNode._mute = mute;
}

function stopTalk() {
  if (!playing) return;
  playing = false;
  $('ptt').classList.remove('active');
  $('ptt').textContent = '按住 说话';
  $('bar').style.width = '0';
  if (micNode) {
    // worklet 有 port，先让它停拉采样再断开，否则残留的回调还会 postMessage
    if (micNode.port) { try { micNode.port.postMessage('stop'); } catch (e) {} micNode.port.onmessage = null; }
    micNode.onaudioprocess = null;
    micNode.disconnect();
    if (micNode._mute) micNode._mute.disconnect();
    micNode = null;
  }
  if (micStream) { micStream.getTracks().forEach(t => t.stop()); micStream = null; }
  sendBuf = [];
}

$('conn').onclick = () => {
  if (ws && ws.readyState === 1) { ws.close(); ws = null; return; }
  connect();
};

// 长按弹菜单:光靠 CSS 挡不住 —— Android Chrome 的「图片/链接菜单」和
// iOS 的 callout 走的是 contextmenu / selectstart 事件,必须显式吃掉。
document.addEventListener('contextmenu', e => e.preventDefault(), {capture: true});
document.addEventListener('selectstart', e => e.preventDefault(), {capture: true});
// 双指缩放会把按钮按住的动作打散,顺手也禁掉
document.addEventListener('gesturestart', e => e.preventDefault(), {capture: true});

const ptt = $('ptt');
// 不要在 touchstart 上 preventDefault:部分机型会连带压制 pointerdown,
// 结果是按钮彻底失灵。滚动/缩放交给 CSS 的 touch-action:none,
// 长按菜单交给 contextmenu + -webkit-touch-callout,各管一段互不干扰。
ptt.addEventListener('pointerdown', e => {
  e.preventDefault();
  // 捕获指针:手指稍微滑出按钮范围也不会丢 pointerup,
  // 否则「松开」事件落到别处,会一直卡在说话状态。
  try { ptt.setPointerCapture(e.pointerId); } catch (err) {}
  startTalk();
});
['pointerup', 'pointercancel', 'lostpointercapture'].forEach(ev =>
  ptt.addEventListener(ev, e => { e.preventDefault(); stopTalk(); }));
window.addEventListener('blur', stopTalk);

// 连上才允许按
const iv = setInterval(() => {
  $('ptt').disabled = !(ws && ws.readyState === 1);
  if (ws && ws.readyState === 1) clearInterval(iv);
}, 300);

connect();
</script>
</body>
</html>
"""

# ---------------------------------------------------------------- 桥接

class Session:
    """一条 WebSocket = 一台网页对讲机。"""

    def __init__(self, reader, writer, room, relay_addr):
        self.reader, self.writer = reader, writer
        self.room = room
        self.relay_addr = relay_addr
        self.codec = OpusCodec()          # 每个会话独占一份编解码状态
        self.peer = None
        self.tx_queue = asyncio.Queue(maxsize=24)   # ~1.4 秒,再深就是给自己加延迟
        self.pcm_buf = bytearray()
        self.rx_frames = 0
        self.rx_bad = 0
        self.tx_drop = 0
        self.ws_sent = 0
        # 上行(手机→设备)统计：帧数、字节、短帧数(帧长不到 1920 说明采集端被卡过)
        self.ul_frames = 0
        self.ul_bytes = 0
        self.ul_short = 0
        self.ul_rms_sum = 0.0
        self.ul_sr = 0
        self.ul_worklet = None

    async def run(self):
        host, port = self.relay_addr
        self.peer = RelayPeer(host, port, self.room, "WEB1", self.codec)
        self.peer.on_audio = self._on_remote_audio
        self.peer.on_event = self._on_event

        try:
            peers = await self.peer.connect()
        except Exception as exc:                      # noqa: BLE001
            await self._send_json({"type": "error", "msg": "中转连接失败: %s" % exc})
            return
        await self._send_json({"type": "peers", "n": peers})
        tx_task = asyncio.ensure_future(self._tx_loop())

        try:
            while True:
                opcode, data = await ws_read(self.reader)
                if opcode == 0x8:                     # close
                    break
                if opcode == 0x9:                     # ping
                    self.writer.write(ws_pack(0xA, data))
                    await self.writer.drain()
                    continue
                if opcode == 0x1:                     # 文本：只认 hello，用来核对浏览器真实采样率
                    self._on_text(data)
                    continue
                if opcode != 0x2:                     # 只处理二进制音频
                    continue
                self._on_mic_pcm(data)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        except asyncio.CancelledError:
            raise
        finally:
            # 不取消的话 asyncio 收尾时会刷 "Task was destroyed but it is pending"，
            # 而且那条协程还挂在 tx_queue 上一直等，白占内存。
            tx_task.cancel()
            await self.peer.close()

    def _on_text(self, data):
        """浏览器上报的采集参数，用来确认上行 PCM 到底是不是 16k。"""
        try:
            msg = json.loads(data.decode("utf-8", "replace"))
        except Exception:                             # noqa: BLE001
            return
        if msg.get("type") != "hello":
            return
        self.ul_sr = int(msg.get("sr") or 0)
        self.ul_worklet = "worklet" if msg.get("wl") else "scriptprocessor"
        log.info("上行采集参数: 浏览器采样率=%dHz 方式=%s %s", self.ul_sr, self.ul_worklet,
                 "" if self.ul_sr == SAMPLE_RATE else "⚠ 不是 16k，已由页面重采样")

    def _on_mic_pcm(self, data):
        """手机上传的 PCM 按 60ms 切帧，编码成 Opus 发给 relay。"""
        self.ul_frames += 1
        if len(data) != PCM_FRAME_BYTES:
            self.ul_short += 1
        # 顺手算 RMS：长期偏低说明手机麦克风增益不够，偏高说明已经削顶
        n = len(data) // 2
        if n:
            s = 0
            for i in range(0, n, 8):                  # 抽样，够用且不占 CPU
                v = int.from_bytes(data[i * 2:i * 2 + 2], "little", signed=True)
                s += v * v
            self.ul_rms_sum += (s / max(1, n // 8)) ** 0.5
        self.pcm_buf.extend(data)
        while len(self.pcm_buf) >= PCM_FRAME_BYTES:
            chunk = bytes(self.pcm_buf[:PCM_FRAME_BYTES])
            del self.pcm_buf[:PCM_FRAME_BYTES]
            frame = self.codec.encode(chunk)
            if frame:
                self.ul_bytes += len(frame)
                self.peer.send_audio(frame)

    def _on_remote_audio(self, opus_frame):
        self.rx_frames += 1
        if self.rx_frames == 1:
            log.info("收到对端音频首帧 %d 字节", len(opus_frame))
        pcm = self.codec.decode(opus_frame)
        if not pcm:
            self.rx_bad += 1
            if self.rx_bad <= 3:
                log.warning("Opus 解码失败(%d 字节,第 %d 次)", len(opus_frame), self.rx_bad)
            return
        try:
            self.tx_queue.put_nowait(pcm)
        except asyncio.QueueFull:
            # 丢最旧的一帧而不是最新的:最新帧代表"现在的声音",
            # 丢掉它反而让播放缓冲塌出空洞,听起来更卡。
            try:
                self.tx_queue.get_nowait()
                self.tx_queue.put_nowait(pcm)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass
            self.tx_drop += 1

    def _on_event(self, ev):
        asyncio.ensure_future(self._send_json(ev))

    async def _send_json(self, obj):
        try:
            self.writer.write(ws_pack(0x1, json.dumps(obj).encode()))
            await self.writer.drain()
        except Exception:                             # noqa: BLE001
            pass

    async def _tx_loop(self):
        try:
            while True:
                pcm = await self.tx_queue.get()
                self.writer.write(ws_pack(0x2, pcm))
                await self.writer.drain()
                self.ws_sent += 1
        except (ConnectionResetError, asyncio.CancelledError):
            pass


async def handle_client(reader, writer, relay_addr):
    addr = writer.get_extra_info("peername")
    ip = addr[0] if addr else "?"
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, asyncio.LimitOverrunError):
        writer.close()
        return

    lines = head.decode("latin-1").split("\r\n")
    request = lines[0] if lines else ""
    parts = request.split()
    path = parts[1] if len(parts) > 1 else "/"

    # 记下真实来源和链路痕迹：走 Lucky 反代时 peer 恒为 127.0.0.1，
    # 走 Cloudflare 隧道时会多出 CF-Ray / CF-Connecting-IP。音频卡不卡跟
    # 走哪条路关系极大，不把这两个头打出来就永远查不清是哪一段在抖。
    via = []
    hdrs = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            hdrs[k.strip().lower()] = v.strip()
    if "cf-ray" in hdrs:
        via.append("CF隧道")
    if "x-forwarded-for" in hdrs:
        via.append("XFF=" + hdrs["x-forwarded-for"][:40])
    if "cf-connecting-ip" in hdrs:
        via.append("CFIP=" + hdrs["cf-connecting-ip"][:40])
    if hdrs.get("x-real-ip"):
        via.append("RealIP=" + hdrs["x-real-ip"][:40])

    if path.startswith("/ws"):
        if not await ws_handshake(reader, writer, lines[1:]):
            return
        room = "1240"
        if "room=" in path:
            room = path.split("room=", 1)[1].split("&")[0][:4]
        room = "".join(c for c in room if c.isdigit()) or "1240"
        log.info("%s 网页端接入,房间 %s %s", ip, room, ("[" + " ".join(via) + "]") if via else "")
        try:
            session = Session(reader, writer, room, relay_addr)
        except Exception as exc:                      # noqa: BLE001
            log.error("Opus 初始化失败,拒绝本会话: %s", exc)
            try:
                writer.write(ws_pack(0x8, b"\x03\xf3"))
            except Exception:                         # noqa: BLE001
                pass
            writer.close()
            return
        try:
            await session.run()
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:                         # noqa: BLE001
                pass
            log.info("%s 网页端离开 (收音频 %d 帧, 解码失败 %d, 下发 %d, 拥塞丢弃 %d)",
                     ip, session.rx_frames, session.rx_bad, session.ws_sent, session.tx_drop)
            if session.ul_frames:
                avg = session.ul_rms_sum / session.ul_frames if session.ul_frames else 0.0
                log.info("  上行: 采集 %d 包(短包 %d, 采样率 %dHz, %s), 编码 %d 帧/均 %.0f 字节, RMS≈%.0f%s",
                         session.ul_frames, session.ul_short, session.ul_sr,
                         session.ul_worklet or "未知",
                         session.ul_frames, session.ul_bytes / session.ul_frames, avg,
                         " (偏小，手机增益不足)" if avg and avg < 300 else
                         (" (偏大，可能削顶)" if avg > 6000 else ""))
        return

    if path in ("/", "/index.html"):
        body = PAGE.encode()
        writer.write(b"HTTP/1.1 200 OK\r\n"
                     b"Content-Type: text/html; charset=utf-8\r\n"
                     b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                     b"Cache-Control: no-store\r\n\r\n" + body)
    elif path == "/health":
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
    else:
        writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
    try:
        await writer.drain()
    finally:
        writer.close()


async def amain(args):
    # 这里只做一次可用性检查;真正给每个会话各建一份 codec(见 Session.__init__)
    try:
        _load_opus()
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1
    log.info("libopus 就绪: %d Hz / %d ch / %d ms 每帧", SAMPLE_RATE, CHANNELS, FRAME_MS)

    relay_addr = (args.relay_host, args.relay_port)
    servers = []
    for host in [h.strip() for h in args.host.split(",") if h.strip()]:
        try:
            srv = await asyncio.start_server(
                lambda r, w: handle_client(r, w, relay_addr), host, args.port)
            servers.append(srv)
        except OSError as exc:
            if exc.errno not in (98, 10048):
                log.warning("监听 %s:%d 失败: %s", host, args.port, exc)

    if not servers:
        log.error("没有可用的监听地址")
        return 1

    for srv in servers:
        for sock in srv.sockets:
            sa = sock.getsockname()
            fam = "IPv6" if sock.family.name == "AF_INET6" else "IPv4"
            log.info("监听 %s %s:%d", fam, sa[0], sa[1])
    log.info("网页打开 http://<本机IP>:%d  (麦克风需要 https 或 localhost)", args.port)

    async with servers[0]:
        await servers[0].serve_forever()


def main():
    ap = argparse.ArgumentParser(description="Passport 对讲机 · 网页版对端")
    ap.add_argument("--host", default="::,0.0.0.0", help="监听地址,逗号分隔")
    ap.add_argument("--port", type=int, default=7802, help="网页端口,默认 7802")
    ap.add_argument("--relay-host", default="127.0.0.1", help="relay.py 地址")
    ap.add_argument("--relay-port", type=int, default=7801, help="relay.py 端口")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S")
    try:
        sys.exit(asyncio.run(amain(args)))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
