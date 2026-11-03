"""Windows 默认音频端点切换（IPolicyConfig COM，未公开接口）。

为什么需要它：微信输入法若跟随「系统默认麦克风」，而默认被别的设备（如
USB dongle）占住，灌进 VB-CABLE 的音频它根本听不到，表现就是设备端
「未听清」/ 取字 0 个，但声卡写入统计完全正常。

用法：
    python audio_default.py list                  # 列出所有录音/播放端点
    python audio_default.py get capture           # 查当前默认录音设备
    python audio_default.py set capture "CABLE"   # 按名字子串切默认录音设备
    python audio_default.py set capture --id {0.0.1.00000000}.{...}

IPolicyConfig 未公开，Windows 7~11 通用；vtable 里 SetDefaultEndpoint 是第 13 项。
"""
import ctypes
import sys
from ctypes import POINTER, byref, c_int, c_uint, c_ulong, c_void_p, c_wchar_p, wintypes

ole32 = ctypes.WinDLL("ole32")

CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
CLSID_CPolicyConfigClient = "{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}"
IID_IPolicyConfig = "{F8679F50-850A-41CF-9C72-430F290290C8}"


class GUID(ctypes.Structure):
    _fields_ = [("Data1", c_ulong), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, s=None):
        super().__init__()
        if s:
            ole32.CLSIDFromString(c_wchar_p(s), byref(self))


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", c_ulong)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", wintypes.USHORT), ("r1", wintypes.USHORT),
                ("r2", wintypes.USHORT), ("r3", wintypes.USHORT),
                ("data", ctypes.c_byte * 16)]


def _vtbl(obj, index, restype, *argtypes):
    """取 COM 对象的第 index 个虚表函数。"""
    vt = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    return proto(vt[index])


def _create(clsid, iid):
    p = c_void_p()
    hr = ole32.CoCreateInstance(byref(GUID(clsid)), None, 1, byref(GUID(iid)), byref(p))
    if hr != 0:
        raise OSError(f"CoCreateInstance 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
    return p


def list_endpoints(flow: str):
    """flow: 'capture' 或 'render'。返回 [(id, name)]，只含已启用端点。"""
    enum = _create(CLSID_MMDeviceEnumerator, IID_IMMDeviceEnumerator)
    coll = c_void_p()
    # EnumAudioEndpoints(dataFlow, stateMask=ACTIVE|DISABLED|UNPLUGGED, &coll)
    hr = _vtbl(enum, 3, ctypes.HRESULT, c_int, wintypes.DWORD, POINTER(c_void_p))(
        enum, 1 if flow == "capture" else 0, 0xF, byref(coll))
    if hr != 0:
        raise OSError(f"EnumAudioEndpoints 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
    count = c_uint()
    _vtbl(coll, 3, ctypes.HRESULT, POINTER(c_uint))(coll, byref(count))
    out = []
    for i in range(count.value):
        dev = c_void_p()
        hr = _vtbl(coll, 4, ctypes.HRESULT, c_uint, POINTER(c_void_p))(coll, i, byref(dev))
        if hr != 0:
            continue
        sid = c_wchar_p()
        _vtbl(dev, 5, ctypes.HRESULT, POINTER(c_wchar_p))(dev, byref(sid))
        # OpenPropertyStore(STGM_READ) → 取 PKEY_Device_FriendlyName
        store = c_void_p()
        name = ""
        hr = _vtbl(dev, 4, ctypes.HRESULT, wintypes.DWORD, POINTER(c_void_p))(dev, 0, byref(store))
        if hr == 0:
            pk = PROPERTYKEY()
            ole32.CLSIDFromString(c_wchar_p("{A45C254E-DF1C-4EFD-8020-67D146A850E0}"),
                                  byref(pk.fmtid))
            pk.pid = 14  # PKEY_Device_FriendlyName
            pv = PROPVARIANT()
            if _vtbl(store, 5, ctypes.HRESULT, POINTER(PROPERTYKEY), POINTER(PROPVARIANT))(
                    store, byref(pk), byref(pv)) == 0 and pv.vt == 31:  # VT_LPWSTR
                name = ctypes.cast(pv.data, c_wchar_p).value or ""
            _vtbl(store, 2, c_ulong)(store)
        out.append((sid.value or "", name))
        _vtbl(dev, 2, c_ulong)(dev)
    _vtbl(coll, 2, c_ulong)(coll)
    _vtbl(enum, 2, c_ulong)(enum)
    return out


def get_default(flow: str) -> tuple[str, str]:
    enum = _create(CLSID_MMDeviceEnumerator, IID_IMMDeviceEnumerator)
    dev = c_void_p()
    # GetDefaultAudioEndpoint(dataFlow, role=eMultimedia, &dev)
    hr = _vtbl(enum, 4, ctypes.HRESULT, c_int, c_int, POINTER(c_void_p))(
        enum, 1 if flow == "capture" else 0, 1, byref(dev))
    if hr != 0:
        _vtbl(enum, 2, c_ulong)(enum)
        return "", ""
    sid = c_wchar_p()
    _vtbl(dev, 5, ctypes.HRESULT, POINTER(c_wchar_p))(dev, byref(sid))
    did = sid.value or ""
    _vtbl(dev, 2, c_ulong)(dev)
    _vtbl(enum, 2, c_ulong)(enum)
    name = dict(list_endpoints(flow)).get(did, "")
    return did, name


def set_default(flow: str, device_id: str) -> None:
    pc = _create(CLSID_CPolicyConfigClient, IID_IPolicyConfig)
    # SetDefaultEndpoint(deviceId, role)：三个 role 都设，避免只改回放通道
    for role in (0, 1, 2):  # eConsole / eMultimedia / eCommunications
        hr = _vtbl(pc, 13, ctypes.HRESULT, c_wchar_p, c_int)(pc, device_id, role)
        if hr != 0:
            raise OSError(f"SetDefaultEndpoint(role={role}) 失败 hr=0x{hr & 0xFFFFFFFF:08X}")
    _vtbl(pc, 2, c_ulong)(pc)


def main() -> int:
    ole32.CoInitialize(None)
    args = sys.argv[1:]
    if not args or args[0] == "list":
        for flow in ("capture", "render"):
            print(f"== {flow} ==")
            for did, nm in list_endpoints(flow):
                print(f"  {nm!r}\n     {did}")
        return 0
    if args[0] == "get":
        flow = args[1] if len(args) > 1 else "capture"
        did, nm = get_default(flow)
        print(f"{flow} 默认: {nm!r}\n  {did}")
        return 0 if did else 1
    if args[0] == "set":
        flow = args[1]
        target = args[2]
        eps = list_endpoints(flow)
        if target == "--id":
            did = args[3]
        else:
            hits = [(d, n) for d, n in eps if target.lower() in n.lower()]
            if not hits:
                print(f"没找到名字含 {target!r} 的端点。现有：")
                for d, n in eps:
                    print(f"  {n!r}")
                return 1
            did = hits[0][0]
        set_default(flow, did)
        print(f"已切换 {flow} 默认为: {dict(eps).get(did, did)!r}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
