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
