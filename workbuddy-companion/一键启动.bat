@echo off
setlocal enabledelayedexpansion

set "PATH=%SystemRoot%\System32;%PATH%"
chcp 936 >nul
title WorkBuddy 一键启动

rem ============================================================
rem  WorkBuddy 一键启动脚本
rem  用法：
rem    双击运行            启动 WorkBuddy（自动装依赖 + 托盘运行 + 连接设备）
rem    -autostart-on       开启开机自启（随 Windows 登录自动运行）
rem    -autostart-off      关闭开机自启
rem    -autostart-status   查看开机自启状态
rem  可改 WEB_PORT 调整 Web UI 端口（默认 8080，避开被占用的 8000）
rem ============================================================

set "DIR=%~dp0"
set "PYTHON="
set "WEB_PORT=8090"

rem ---------- 定位 Python（python 与 pythonw 同目录，保证依赖环境一致） ----------
set "PYEXE="
for /f "delims=" %%i in ('where python 2^>nul') do if not defined PYEXE set "PYEXE=%%i"
if not defined PYEXE for /f "delims=" %%i in ('where py 2^>nul') do if not defined PYEXE set "PYEXE=%%i"
if not defined PYEXE if exist "%LocalAppData%\Programs\Python\Python311\python.exe" set "PYEXE=%LocalAppData%\Programs\Python\Python311\python.exe"
if not defined PYEXE if exist "%USERPROFILE%\AppData\Roaming\WPS 灵犀\python-env\python.exe" set "PYEXE=%USERPROFILE%\AppData\Roaming\WPS 灵犀\python-env\python.exe"
if not defined PYEXE (
    echo [错误] 未找到 Python，请先安装 Python 3.11+ 并加入 PATH。
    pause
    exit /b 1
)
for %%D in ("%PYEXE%") do set "PYWEXE=%%~dpDpythonw.exe"
cd /d "%DIR%"

rem ---------- 开机自启开关（参数调用 wb_app.py 完成） ----------
if /i "%~1"=="-autostart-on"   goto autostart_on
if /i "%~1"=="-autostart-off"  goto autostart_off
if /i "%~1"=="-autostart-status" goto autostart_status

rem ---------- 一键启动：装依赖 + 托盘运行 ----------
rem 三个第三方依赖缺一不可：bleak（BLE 通信）、pystray（托盘）、Pillow（托盘图标）。
rem 缺 bleak 时程序仍能启动，但永远连不上设备，故必须在此拦住。
echo 正在检查依赖（首次会自动安装 bleak / pystray / Pillow，仅需几秒）...
"%PYEXE%" -c "import bleak, pystray, PIL" >nul 2>nul || (
    echo 检测到缺失依赖，正在安装 bleak / pystray / Pillow ...
    "%PYEXE%" -m pip install -q bleak pystray Pillow || (
        echo [错误] 依赖安装失败，请手动执行： "%PYEXE%" -m pip install bleak pystray Pillow
        pause
        exit /b 1
    )
)

echo 正在启动 WorkBuddy 控制台（托盘运行，Web UI: http://127.0.0.1:%WEB_PORT%）
echo 关闭本窗口不影响运行，可随时从托盘退出。
start "" "%PYWEXE%" "wb_app.py" --tray --web-port %WEB_PORT% --open-browser
exit /b 0

:autostart_on
cd /d "%DIR%"
"%PYEXE%" "wb_app.py" --autostart-on
echo.
echo 已开启开机自启。此后 Windows 登录会自动运行 WorkBuddy（托盘模式）。
pause
exit /b 0

:autostart_off
cd /d "%DIR%"
"%PYEXE%" "wb_app.py" --autostart-off
echo.
echo 已关闭开机自启。
pause
exit /b 0

:autostart_status
cd /d "%DIR%"
"%PYEXE%" "wb_app.py" --autostart-status
echo.
pause
exit /b 0
