"""WorkBuddy companion boot launcher.

Registered under HKCU Run and executed by pythonw.exe at logon.

Two problems this solves, both of which make a direct Run-key launch
fail silently (pythonw has no console, so nothing is ever printed):

1. Timing - the tray icon cannot be created while Explorer is still
   starting up, so the process died right after logon.
2. Missing standard streams - a process started from the Run key has no
   stdout/stderr handles, which makes ``sys.stdout`` None. wb_app.py uses
   ``print()`` extensively, and the very first call raised
   AttributeError and killed the process.

So: wait for the shell to settle, open a log file, point the standard
streams at it, then run wb_app.py in this same process.
"""
import datetime
import io
import os
import runpy
import socket
import sys
import time
import traceback

BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE, "wb_autostart.log")
APP = os.path.join(BASE, "wb_app.py")
WAIT_SECONDS = 20
WEB_PORT = 8090  # 8080 is reserved for the local LLM server on this machine
MUTEX_NAME = "Local\\WorkBuddyCompanionDaemon-v1"
_MUTEX_HANDLE = None


def acquire_single_instance() -> bool:
    """拿命名互斥体，保证同一会话内只有一个守护实例。

    2026-11-05 定位：HKCU Run 与计划任务两个入口会在开机时同时启动，
    而端口检查发生在 20 秒等待之后，两者几乎同时通过检查 —— 结果起了两个
    实例，任务栏出现两个图标，后一个还因 IPC 48765 抢占失败变成半死进程。
    互斥体在等待之前获取，天然消除这个竞态。
    """
    global _MUTEX_HANDLE
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not handle:
            return True  # 创建失败就放行：宁可重复一次，也不要完全起不来
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            return False
        _MUTEX_HANDLE = handle  # 持有引用，进程存活期间不释放
        return True
    except Exception:
        return True


def log(message: str) -> None:
    try:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")
    except OSError:
        pass


def port_in_use(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        return sock.connect_ex(("127.0.0.1", port)) == 0
    finally:
        sock.close()


def main() -> int:
    if not acquire_single_instance():
        log("boot launcher: another instance already running, skip")
        return 0
    log(f"boot launcher: start, waiting {WAIT_SECONDS}s for shell/network")
    time.sleep(WAIT_SECONDS)

    if port_in_use(WEB_PORT):
        log(f"boot launcher: port {WEB_PORT} already in use, skip")
        return 0
    if not os.path.exists(APP):
        log(f"boot launcher: ERROR wb_app.py missing at {APP}")
        return 1

    # A Run-key process owns no std handles; give wb_app.py a writable sink.
    try:
        sink = open(LOG, "a", encoding="utf-8", buffering=1)
    except OSError:
        return 1
    sys.stdout = sink
    sys.stderr = sink
    if sys.stdin is None:
        sys.stdin = io.StringIO("")

    os.chdir(BASE)
    sys.argv = [APP, "--tray", "--web-port", str(WEB_PORT)]
    log(f"boot launcher: entering wb_app.py --tray --web-port {WEB_PORT}")
    try:
        runpy.run_path(APP, run_name="__main__")
    except SystemExit as exc:
        log(f"boot launcher: wb_app exited ({exc.code})")
        return int(exc.code or 0)
    except BaseException:
        log("boot launcher: CRASH\n" + traceback.format_exc())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
