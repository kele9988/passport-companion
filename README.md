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

**快速开始**（服务端机器上）：

```bash
# 1. 启动房间中转（监听 7801，可设 4 字符房间口令）
python3 relay.py --host :: --port 7801 --token 8888

# 2.（可选）启动网页对端（监听 7802，供浏览器使用）
python3 gateway.py --port 7802 --relay 127.0.0.1:7801
```

**设备侧配置**：对讲机应用 → 设置中转主机（填本服务器的域名或 IP）与房间号，两台设备配同一房间即可通话。

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

## 许可证

[MIT](LICENSE)
