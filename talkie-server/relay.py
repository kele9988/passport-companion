#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Passport 对讲机 · 中转服务

在飞牛 NAS(或任意有公网 IPv4/IPv6 的 Linux 机器)上跑这一支即可:

    python3 relay.py --port 7801

它只做三件事:
  1. 设备凭房间号加入(JOIN)
  2. 同房间内,一端的 Opus 音频帧转发给其他人(AUDIO)
  3. 人数 / 谁在讲话 的变化推给同房间所有人(PEERS / TALK)
     界面上那句「信道1 在线3」的 3 就是 PEERS

不解析音频内容、不解码、不落盘,纯转发。整个文件零第三方依赖。

协议(TCP,全部大端)
    每帧 6 字节头 + payload
        0..1  magic  b'TB'
        2     type
        3..4  payload 长度 (u16)
        5     seq    设备自增,服务器原样透传,便于排查丢帧

    type:
        0x01 JOIN       payload: room(4 ascii) devid(4) token(4)
        0x81 JOIN_ACK   payload: result(1) peers(1) self_id(u32)
                        result: 0 成功 / 1 房间满 / 2 协议不符 / 3 口令错
        0x02 AUDIO      payload: 一帧 Opus(设备侧约 240 字节)
        0x03 PING       payload: 空
        0x83 PONG       payload: 空
        0x84 PEERS      payload: count(1)
        0x05 TALK       设备发给服务器,payload: state(1) 1=开始 0=结束
        0x85 TALK       服务器转发,  payload: state(1) + pid(u32)

为什么是 TCP 不是 UDP:
    C3 的剩余堆只有十几 KB,开 DTLS 必挂;裸 UDP 又得自己做重传、拥塞、乱序。
    一帧 Opus 240 字节、60ms 一发,相当 4KB/s,TCP 完全带得动。代价是队头阻塞,
    所以发送端关 Nagle(TCP_NODELAY),接收端做 2 帧抖动缓冲,实测足够。
"""

import argparse
import asyncio
import contextlib
import logging
import signal
import struct
import sys
from collections import defaultdict

MAGIC = b"TB"
HEADER = struct.Struct(">2sBHB")   # magic, type, payload_len, seq
HEADER_LEN = HEADER.size           # 6

T_JOIN = 0x01
T_JOIN_ACK = 0x81
T_AUDIO = 0x02
T_PING = 0x03
T_PONG = 0x83
T_PEERS = 0x84
T_TALK_REQ = 0x05
T_TALK = 0x85

R_OK, R_FULL, R_VERSION, R_TOKEN = 0, 1, 2, 3

MAX_FRAME = 1024        # 远大于一帧 Opus,越界一律当坏包
JOIN_TIMEOUT = 10.0     # 连上后多久不 JOIN 就踢
IDLE_TIMEOUT = 45.0     # 多久没有任何帧就判死链(设备 15s 一次心跳)
SEND_QUEUE = 48         # 每客户端待发队列深度,约 3 秒音频

log = logging.getLogger("relay")


def build(ftype, payload=b"", seq=0):
    return HEADER.pack(MAGIC, ftype, len(payload), seq & 0xFF) + payload


async def read_frame(reader):
    hdr = await reader.readexactly(HEADER_LEN)
    magic, ftype, length, seq = HEADER.unpack(hdr)
    if magic != MAGIC:
        raise ValueError("bad magic %r" % (magic,))
    if length > MAX_FRAME:
        raise ValueError("frame too large: %d" % length)
    payload = await reader.readexactly(length) if length else b""
    return ftype, payload, seq


class Peer:
    __slots__ = ("reader", "writer", "ip", "room", "devid", "pid", "queue",
                 "sender", "dropped", "talking")

    def __init__(self, reader, writer, ip):
        self.reader = reader
        self.writer = writer
        self.ip = ip
        self.room = b""
        self.devid = b""
        self.pid = 0
        self.queue = asyncio.Queue(maxsize=SEND_QUEUE)
        self.sender = None
        self.dropped = 0
        self.talking = False

    @property
    def name(self):
        d = self.devid.decode("ascii", "replace").strip() or "????"
        return "%s@%s" % (d, self.ip)


class Relay:
    def __init__(self, room_limit=8, token=b""):
        self.room_limit = room_limit
        self.token = token
        self.rooms = defaultdict(dict)   # room(bytes) -> {pid: Peer}
        self._next_id = 1

    # ---------- 收发 ----------
    async def send_loop(self, peer):
        """每个客户端一条独立发送协程,避免一个慢客户端卡住整个房间的转发。"""
        try:
            while True:
                data = await peer.queue.get()
                if data is None:
                    break
                peer.writer.write(data)
                await peer.writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        except Exception as exc:                      # noqa: BLE001
            log.debug("%s 发送协程退出: %s", peer.name, exc)

    def push(self, peer, data):
        """非阻塞投递。队列满说明对端跟不上,丢最旧的一帧——对讲机宁可丢帧也要保实时。"""
        if peer.queue.full():
            try:
                peer.queue.get_nowait()
                peer.dropped += 1
            except asyncio.QueueEmpty:
                pass
        try:
            peer.queue.put_nowait(data)
        except asyncio.QueueFull:
            pass

    def broadcast_peers(self, room):
        members = self.rooms.get(room) or {}
        count = len(members)
        pkt = build(T_PEERS, struct.pack(">B", min(count, 255)))
        for p in members.values():
            self.push(p, pkt)
        return count

    def forward_audio(self, sender, data):
        members = self.rooms.get(sender.room) or {}
        for p in list(members.values()):
            if p.pid != sender.pid:
                self.push(p, data)

    # ---------- 连接生命周期 ----------
    async def handle(self, reader, writer):
        sock = writer.get_extra_info("socket")
        if sock is not None:
            try:
                import socket as _s
                sock.setsockopt(_s.IPPROTO_TCP, _s.TCP_NODELAY, 1)
                sock.setsockopt(_s.SOL_SOCKET, _s.SO_KEEPALIVE, 1)
            except OSError:
                pass

        addr = writer.get_extra_info("peername")
        ip = addr[0] if addr else "?"
        if ip.startswith("::ffff:"):
            ip = ip[7:]           # 双栈监听时 IPv4 会以 v4-mapped 形式出现
        peer = Peer(reader, writer, ip)
        peer.sender = asyncio.ensure_future(self.send_loop(peer))
        joined = False

        try:
            ftype, payload, _ = await asyncio.wait_for(read_frame(reader), JOIN_TIMEOUT)
            if ftype != T_JOIN or len(payload) < 12:
                log.warning("%s 首帧不是合法 JOIN,断开", ip)
                self.push(peer, build(T_JOIN_ACK, struct.pack(">BBI", R_VERSION, 0, 0)))
                await writer.drain()
                return

            room, devid, token = payload[0:4], payload[4:8], payload[8:12]
            peer.room, peer.devid = room, devid

            if self.token and token != self.token:
                log.warning("%s 口令不符(room=%s)", ip, room.decode("ascii", "replace"))
                self.push(peer, build(T_JOIN_ACK, struct.pack(">BBI", R_TOKEN, 0, 0)))
                await writer.drain()
                return

            members = self.rooms[room]
            if len(members) >= self.room_limit:
                log.warning("房间 %s 已满(%d),拒绝 %s",
                            room.decode("ascii", "replace"), len(members), ip)
                self.push(peer, build(T_JOIN_ACK,
                                      struct.pack(">BBI", R_FULL, len(members), 0)))
                await writer.drain()
                return

            # 同 devid 顶替：网页端重连或旧连接僵死时，同一台机会在房间里
            # 堆出多个会话、彼此转发音频，曾把 gateway 下行队列塞爆（一次会话
            # 拥塞丢弃 5 万帧，对端全程无声）。新连接 JOIN 时直接踢掉同房间
            # 同 devid 的旧连接，保证一台设备/一个网页端只占一个位置。
            for stale in [p for p in list(members.values()) if p.devid == devid]:
                log.info("[%s] %s 被新连接顶替,断开旧连接",
                         room.decode("ascii", "replace"), stale.name)
                members.pop(stale.pid, None)
                with contextlib.suppress(Exception):
                    stale.queue.put_nowait(None)
                if stale.sender:
                    stale.sender.cancel()
                with contextlib.suppress(Exception):
                    stale.writer.close()
            peer.pid = self._next_id
            self._next_id = self._next_id % 0xFFFFFFFF + 1
            members[peer.pid] = peer
            joined = True

            rname = room.decode("ascii", "replace")
            log.info("[%s] %s 加入,在线 %d", rname, peer.name, len(members))
            self.push(peer, build(T_JOIN_ACK,
                                  struct.pack(">BBI", R_OK, min(len(members), 255), peer.pid)))
            await writer.drain()
            self.broadcast_peers(room)

            while True:
                ftype, payload, seq = await asyncio.wait_for(read_frame(reader), IDLE_TIMEOUT)

                if ftype == T_AUDIO:
                    self.forward_audio(peer, build(T_AUDIO, payload, seq))

                elif ftype == T_PING:
                    # 必须把 PING 带的发送时刻原样回显:设备端就是拿它算 RTT 的
                    # (见 talkie_link.cc 里 kTypePong 的 len>=8 分支)。
                    # 早先这里回的是空负载,设备拿不到时间戳,界面永远显示 --ms。
                    self.push(peer, build(T_PONG, payload, seq))

                elif ftype == T_TALK_REQ:
                    state = 1 if (payload and payload[0]) else 0
                    if peer.talking == bool(state):
                        continue
                    peer.talking = bool(state)
                    pkt = build(T_TALK, struct.pack(">BI", state, peer.pid))
                    for p in list(self.rooms[room].values()):
                        if p.pid != peer.pid:
                            self.push(p, pkt)

                else:
                    log.debug("%s 未知帧类型 0x%02x,忽略", peer.name, ftype)

        except asyncio.TimeoutError:
            log.info("%s 超时未活动,断开", peer.name if joined else ip)
        except asyncio.IncompleteReadError:
            pass
        except (ConnectionResetError, BrokenPipeError):
            pass
        except ValueError as exc:
            log.warning("%s 协议错误: %s", peer.name if joined else ip, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                       # noqa: BLE001
            log.exception("%s 异常: %s", peer.name if joined else ip, exc)
        finally:
            if joined:
                members = self.rooms.get(peer.room)
                if members is not None:
                    members.pop(peer.pid, None)
                    count = len(members)
                    log.info("[%s] %s 离开,在线 %d",
                             peer.room.decode("ascii", "replace"), peer.name, count)
                    if count:
                        self.broadcast_peers(peer.room)
                        if peer.talking:
                            pkt = build(T_TALK, struct.pack(">BI", 0, peer.pid))
                            for p in list(members.values()):
                                self.push(p, pkt)
                    else:
                        self.rooms.pop(peer.room, None)   # 空房间及时回收
            elif not peer.devid:
                log.debug("%s 未完成握手就断开", ip)

            try:
                peer.queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            if peer.sender:
                peer.sender.cancel()
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass

    # ---------- 运维 ----------
    async def stats_loop(self, period=60.0):
        while True:
            await asyncio.sleep(period)
            if not self.rooms:
                log.info("当前无房间在线")
                continue
            parts = []
            for room, members in self.rooms.items():
                drop = sum(p.dropped for p in members.values())
                parts.append("%s:%d人%s" % (room.decode("ascii", "replace"), len(members),
                                            "(丢%d)" % drop if drop else ""))
            log.info("在线房间 %d 个 → %s", len(self.rooms), " | ".join(parts))


async def amain(args):
    relay = Relay(room_limit=args.rooms,
                  token=args.token.encode("ascii")[:4].ljust(4, b"\0") if args.token else b"")

    # 每个地址单独起一个 listener,而不是把一个列表塞进一次 start_server:
    # Windows 上绑 "::" 拿到的套接字不接受 IPv4 映射连接(实测 v4 连过来是
    # WSAECONNREFUSED),Linux 上 "::" 则是双栈、再绑 0.0.0.0 会 EADDRINUSE。
    # 分开绑、各自 try,两种系统都能同时收 v4 和 v6。
    servers = []
    last_err = None
    for host in [h.strip() for h in args.host.split(",") if h.strip()]:
        try:
            srv = await asyncio.start_server(relay.handle, host, args.port)
            servers.append(srv)
        except OSError as exc:
            last_err = exc
            # 双栈系统上第二个 bind 必然是 EADDRINUSE,属正常,不刷警告
            if exc.errno not in (98, 10048):
                log.warning("监听 %s:%d 失败: %s", host, args.port, exc)

    if not servers:
        log.error("所有监听地址都失败: %s", last_err)
        return 1

    for srv in servers:
        for sock in srv.sockets:
            sa = sock.getsockname()
            fam = "IPv6" if sock.family.name == "AF_INET6" else "IPv4"
            log.info("监听 %s %s:%d", fam, sa[0], sa[1])
    log.info("房间上限 %d 人/间,口令 %s", relay.room_limit,
             "已启用" if relay.token else "关闭")

    stats = asyncio.ensure_future(relay.stats_loop())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass            # Windows 上部分信号不支持,靠 KeyboardInterrupt

    async with contextlib.AsyncExitStack() as stack:
        for srv in servers:
            await stack.enter_async_context(srv)
        try:
            await stop.wait()
        except asyncio.CancelledError:
            pass

    log.info("正在关闭…")
    stats.cancel()
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Passport 对讲机中转服务",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例:\n"
               "  python3 relay.py --port 7801\n"
               "  python3 relay.py --host :: --port 7801 --token 8888\n"
               "  python3 relay.py --host ::,0.0.0.0 --port 7801")
    ap.add_argument("--host", default="::,0.0.0.0",
                    help="监听地址,逗号分隔。:: 收 IPv6(飞牛 DDNS 用),0.0.0.0 收 IPv4。默认两者都试")
    ap.add_argument("--port", type=int, default=7801, help="监听端口,默认 7801")
    ap.add_argument("--rooms", type=int, default=8, help="每个房间最多几人,默认 8")
    ap.add_argument("--token", default="", help="4 字符房间口令,留空即不校验")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印调试日志")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        sys.exit(asyncio.run(amain(args)))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
