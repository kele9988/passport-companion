# talkie-server — 对讲机自建中转服务器指南

给 Passport OS「对讲机 · 异地模式」自建中转服务。三个文件、一台机器就能跑：

| 文件 | 作用 | 依赖 | 端口 |
| --- | --- | --- | --- |
| `relay.py` | 房间中转（必需）：按房间号互转 Opus 音频帧、推送在线人数/讲话状态 | 无（纯标准库） | TCP 7801 |
| `gateway.py` | 网页对端（可选）：手机/电脑浏览器变成第二台对讲机 | 无（纯标准库） | TCP 7802 |
| `ws_talk_bridge.py` | WebSocket 桥（可选）：让没有公网 IP 的家庭宽带也能被设备连上 | `pip install websockets` | TCP 7814 |

服务端只转发加密前的 Opus 音频帧（每帧约 240 字节、60ms 一发），**不解析、不落盘、不存任何用户数据**。

```
设备 A ──TCP 7801──┐
                   ├── relay.py（按房间互转）──┐
设备 B ──TCP 7801──┘                          ├── 同房间互转
浏览器 ──WebSocket 7802── gateway.py ─────────┘
（无公网 IP 时）设备 ──wss://域名── Cloudflare Tunnel ── ws_talk_bridge.py:7814 ── relay.py:7801
```

---

## 一、快速开始（局域网 / NAS，5 分钟）

适合两台设备经常在同一家庭网络、或自已有 NAS（群晖/飞牛/UNRAID 均可）。

```bash
# 1. 把 talkie-server 目录拷到机器上，启动房间中转
python3 relay.py --host :: --port 7801

# 2.（可选）启动网页对端，浏览器变成第二台对讲机
python3 gateway.py --port 7802 --relay 127.0.0.1:7801
```

然后：

- **设备侧**：对讲机 → 长按下键切到**异地**模式 → 双击下键选中房间号（待命后上下键加减，2 秒不按生效）；
- 服务器地址（主机名/IP）目前需一次性写入设备：USB 连电脑，用任一串口工具（115200）发送
  `y192.168.x.x:7801` 回车（`y` 是命令前缀，后面跟 `主机:端口`），再发 `r1248` 设置房间号。写入 NVS 永久保存，断电不丢；
- **浏览器侧**：打开 `http://<服务器IP>:7802`。注意浏览器只允许 HTTPS 或 localhost 使用麦克风，局域网 IP 直接打开拿不到录音权限（用 `localhost` 测试，或挂 HTTPS 反代）。

同一房间的设备互相能听到；顶栏「在线 N」就是 `relay.py` 推送的人数。

---

## 二、部署到公网 VPS（异地通话推荐）

有公网 IPv4 或 IPv6 的 VPS 是最省心的方案：

```bash
# 上传 talkie-server 后
python3 relay.py --host :: --port 7801 --token 8888
```

- `--token 8888`：4 字符房间口令，设备的房间口令一致才能入房（JOIN_ACK 返回 3 = 口令错）。留空则不校验——**公网部署建议设置**，避免房间被陌生人占用；
- `--rooms 8`：每个房间最多 8 人，可调；
- 防火墙放行 **TCP 7801**（relay）；用网页对端再放行 **TCP 7802**；
- IPv4 与 IPv6 同时监听（`--host ::,0.0.0.0`），设备优先 IPv6 直连。

### 常驻运行（systemd）

```ini
# /etc/systemd/system/talkie-relay.service
[Unit]
Description=Talkie relay
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/talkie-server/relay.py --host :: --port 7801 --token 8888
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now talkie-relay
```

`gateway.py` 同理写一个 service 即可（`After=talkie-relay.service`）。

---

## 三、家庭宽带无公网 IP：两条路

### 路 A：IPv6 + DDNS（零额外组件）

国内家宽大多有公网 IPv6，给 NAS/路由器做 DDNS（lucky、Cloudflare API 均可），把域名 AAAA 记录指向家庭 IPv6，防火墙放行 TCP 7801，设备直接 `y你的域名:7801` 写入即可。

限制：设备的网络必须有 IPv6（手机热点一般没有），纯 IPv4 网络下连不上——这时走路 B。

### 路 B：Cloudflare Tunnel + WS 桥（本文仓库自带 `ws_talk_bridge.py`）

原理：设备连 `wss://你的域名`（Cloudflare 边缘同时有 v4/v6，设备永远连得上），流量经隧道回到家里，`ws_talk_bridge.py` 把 WebSocket 二进制帧原样转给本机 `relay.py`，TB 帧协议不变。

```bash
# 1. 家里常驻机器上
pip install websockets
python3 ws_talk_bridge.py            # 监听 7814 -> 127.0.0.1:7801

# 2. Cloudflare 隧道指向 ws_talk_bridge（Zero Trust 控制台或 cloudflared 配置）
cloudflared tunnel --url http://localhost:7814
# 或命名隧道：ingress 里把你的域名 -> http://localhost:7814
```

设备侧写入 `y你的域名`（HTTPS/WSS 由 Cloudflare 自动终结，设备用 wss 访问 443 即可）。

> 注意：Cloudflare 免费版对 WebSocket 有正常的流量与并发限制，对讲流量约 4KB/s，两三人房间完全够用；但请勿把隧道入口分享给不信任的人。

---

## 四、安全提醒

1. **公网部署务必设 `--token`**：4 字符口令虽然简单，但能挡住端口扫描者的随机入房；
2. relay/gateway/bridge 都**没有管理界面、没有鉴权后的管理能力**，暴露的只是"进房间转发音频"这一个动作，风险面很小，但仍建议只放行需要的端口；
3. 服务端不落盘、不解码音频；如需更强的隐私，可把整套服务架在自己完全控制的机器上；
4. 转手/送人设备前，记得清掉设备 NVS 里的对讲配置（`host`/`room`/`token`）。

---

## 五、协议速查（写给想自己实现服务端的人）

TCP，全部大端，每帧 6 字节头 + payload：

```
0..1  magic  b'TB'
2     type
3..4  payload 长度 (u16)
5     seq    设备自增序号，服务器原样透传
```

| type | 方向 | payload |
| --- | --- | --- |
| 0x01 JOIN | 设备→服务器 | room(4 ascii) + devid(4) + token(4) |
| 0x81 JOIN_ACK | 服务器→设备 | result(1) + peers(1) + self_id(u32)；result：0 成功 / 1 房间满 / 2 协议不符 / 3 口令错 |
| 0x02 AUDIO | 双向 | 一帧 Opus（设备侧约 240 字节） |
| 0x03 / 0x83 PING/PONG | 双向 | 空（保活） |
| 0x84 PEERS | 服务器→设备 | count(1)（界面「在线 N」来源） |
| 0x05 / 0x85 TALK | 双向 | state(1) + pid(u32)（谁在讲话） |

发送端已关 Nagle（TCP_NODELAY），接收端 2 帧抖动缓冲，60ms 帧间隔下 TCP 完全够用。
固件侧实现见 passport-os 仓库 `main/apps/talkie/talkie_link.cc` 与 `talkie_proto.h`。
