#!/usr/bin/env python3
# Talkie WS bridge: cloudflared tunnel -> WS(:7814) -> relay TCP 127.0.0.1:7801
# 用途：家庭宽带没有公网 IPv4/IPv6 时的兜底入口 —— cloudflared 把自己的域名
# 指到本机的 7814，IPv4-only 网络下的设备走 wss://<你的域名> 进来，这里做
# WebSocket 二进制帧 <-> TCP 的透明双向转发，TB 帧协议原样透传。
# 依赖：pip install websockets（仅此一个第三方包；relay/gateway 均零依赖）
import asyncio

UPSTREAM = ("127.0.0.1", 7801)  # 本机 relay.py 的地址与端口


async def handler(ws):
    try:
        up_reader, up_writer = await asyncio.wait_for(
            asyncio.open_connection(*UPSTREAM), timeout=5)
    except Exception:
        await ws.close(code=1013)
        return
    print("bridge: device linked to relay", flush=True)
    try:
        async def ws2tcp():
            async for msg in ws:
                if isinstance(msg, str):
                    msg = msg.encode()
                up_writer.write(msg)
                await up_writer.drain()

        async def tcp2ws():
            while True:
                data = await up_reader.read(4096)
                if not data:
                    break
                await ws.send(data)

        await asyncio.gather(ws2tcp(), tcp2ws())
    except Exception:
        pass
    finally:
        try:
            up_writer.close()
        except Exception:
            pass
        print("bridge: device link closed", flush=True)


async def main():
    import websockets
    async with websockets.serve(handler, "::", 7814, max_size=1 << 20):
        print("bridge: ws ready on [::]:7814 ->", UPSTREAM, flush=True)
        await asyncio.Future()


asyncio.run(main())
