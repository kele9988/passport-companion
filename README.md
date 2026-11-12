# Passport Companion — Passport OS 配套服务与工具

为 [Passport OS](https://github.com/manchunx7-bit/passport-os)（FoloToy AI Passport 社区固件）提供的配套开源组件，包含两部分：

```
passport-companion/
├── talkie-server/         对讲机中转服务端（异地通话）
└── workbuddy-companion/   WorkBuddy 电脑端控制台（语音输入/快捷键/监控页）
```

## talkie-server — 对讲机中转服务端

让两台设备在**异地**也能对讲的中转服务，纯 Python 标准库、零第三方依赖，拷贝到任意有公网 IPv6/IPv4 的机器上即可运行。

| 文件 | 作用 |
| --- | --- |
| `relay.py` | 房间中转：设备/网关都以 TCP 客户端接入，按房间号互转 Opus 音频帧 |
| `gateway.py` | 网页对端：把手机/电脑浏览器变成第二台对讲机（WebSocket PCM ↔ libopus） |
| `ws_talk_bridge.py` | WS 桥：家庭宽带无公网 IP 时，经 Cloudflare Tunnel 让设备也能连上（依赖 `websockets`） |

**完整部署指南（局域网 / VPS / 无公网 IP 三种方案 + systemd + 协议速查）**：见 [`talkie-server/README.md`](talkie-server/README.md)。

**快速开始**（服务端机器上）：

```bash
# 1. 启动房间中转（监听 7801，可设 4 字符房间口令）
python3 relay.py --host :: --port 7801 --token 8888

# 2.（可选）启动网页对端（监听 7802，供浏览器使用）
python3 gateway.py --port 7802 --relay 127.0.0.1:7801
```

**设备侧配置**：对讲机 → 长按下键切到异地模式 → 双击下键选中房间号，待命后上下键加减（写入 NVS，重启保留）。服务器地址目前需一次性写入：USB 连电脑，串口工具（115200）发 `y<主机:端口>`（如 `yrelay.example.com:7801`）、`r<4位房间号>`，NVS 永久保存。

**浏览器侧**：浏览器要求 HTTPS 或 localhost 才能给麦克风授权，局域网用 `http://<ip>:7802` 打开拿不到录音权限，请用 localhost 或 HTTPS 反代（Lucky/Cloudflare 等）。

> 近场对讲（两台设备直连）不依赖本服务，固件内置，无需任何配置。

## workbuddy-companion — WorkBuddy 电脑端控制台

把 Passport 设备变成电脑的语音控制台：按住设备说话键语音输入到电脑、配置快捷键、查看电脑状态页（CPU/GPU 温度等）。设备通过蓝牙（BLE）或 Wi-Fi TCP 连接电脑。

| 文件 | 作用 |
| --- | --- |
| `wb_app.py` | 主程序：本地 Web 控制台 + 设备连接 + 语音/快捷键 + 监控页 |
| `wb_boot.py` | 开机自启启动器（注册到 HKCU Run，解决 pythonw 无控制台导致的静默失败） |
| `audio_default.py` / `cable_loopback.py` / `vbcable_selftest.py` | 虚拟声卡（VB-CABLE）相关辅助脚本 |
| `一键启动.bat` | Windows 一键启动（自动装依赖 + 启动服务 + 连接设备），支持 `-autostart-on/off/status` |
| `修复蓝牙.bat` | 重启 Windows 蓝牙服务（BLE 扫描失效时用，需管理员运行） |

**快速开始**（Windows 电脑上）：

1. 安装 Python 3.11+，双击 `一键启动.bat`（自动定位 Python、安装依赖、启动服务并连接设备）；
2. 浏览器打开 `http://localhost:8090` 进入控制台；
3. 在控制台里配对设备（蓝牙或与电脑同局域网的 Wi-Fi 直连）。

**可选组件**：

- VB-CABLE 虚拟声卡驱动（音频回环/自检脚本需要）：[vb-audio.com/Cable](https://vb-audio.com/Cable/)
- 语音输入到第三方应用等 AI 功能需要自行接入凭据（本仓库不包含任何凭据模块）。

**远程访问（进阶）**：控制台默认绑定 `0.0.0.0:8090`（`--web-host` 可改），便于与 Cloudflare Tunnel 等反代配合。异地打开控制台的思路：在家里常驻机器上运行 `cloudflared tunnel --url http://localhost:8090`（或配置命名隧道 + 自己的域名），隧道出口即控制台。注意控制台页面本身没有登录鉴权，公网暴露请务必叠加 Cloudflare Access 等访问控制，不要裸奔。

## 许可证

[MIT](LICENSE)
