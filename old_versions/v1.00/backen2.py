#!/usr/bin/env python3
"""
html_backend_builder.py  -  Termux / Linux / Windows / macOS

Gives any HTML page a backend that saves form input to a JSON file.

  * Serves your HTML at http://127.0.0.1:8000 (page works unchanged)
  * Saves every submit (button click OR Enter key) to <name>_data.json
    in the same folder as the HTML file
  * Passwords are saved as clean text (as typed). Use --hash-passwords
    if you want them hashed instead.

Usage:
    python html_backend_builder.py page.html
    python html_backend_builder.py page.html --port 5000
    python html_backend_builder.py page.html --hash-passwords
    python html_backend_builder.py            (asks for the path)

Standard library only - no pip install needed.
"""
import argparse
import hashlib
import json
import mimetypes
import os
import re
import secrets
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

MAX_BODY = 1_000_000
PBKDF2_ROUNDS = 200_000
LOCK = threading.Lock()
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
BLOCKED_EXT = {".py", ".json", ".env", ".db", ".sqlite"}

# Injected into the page. Captures button clicks, Enter key and form submits,
# de-duplicates them, and posts the values to /api/save.
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
    if (body === lastSig && now - lastTime < 1500) return;   // duplicate event
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


# ------------------------------------------------------------ helpers ----
def setup_console():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


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
    """Lists detected inputs at startup (display only)."""
    def __init__(self):
        super().__init__()
        self.found = []

    def handle_starttag(self, tag, attrs):
        if tag in ("input", "textarea", "select"):
            a = dict(attrs)
            t = (a.get("type") or tag).lower()
            if t not in ("submit", "button", "reset", "image", "file", "hidden"):
                self.found.append((a.get("name") or a.get("id") or a.get("placeholder") or t, t))


# ------------------------------------------------------------ storage ----
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
                print(f"[warn] bad JSON moved to {backup.name}")
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


# ------------------------------------------------------------- server ----
class Handler(BaseHTTPRequestHandler):
    server_version = "HtmlBackend/2.0"

    def log_message(self, fmt, *args):
        print("[http]", fmt % args)

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
        print(f"[saved] record #{total} ({record['form']}): {list(record['data'])}")
        self._send(200, {"ok": True, "id": record["id"], "total": total})


def main():
    setup_console()
    ap = argparse.ArgumentParser(description="Build a JSON-saving backend for an HTML file")
    ap.add_argument("html", nargs="?", help="path to the HTML file")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1",
                    help="default 127.0.0.1 (this device only); 0.0.0.0 for LAN/testers")
    ap.add_argument("--hash-passwords", action="store_true",
                    help="hash passwords (PBKDF2) instead of saving them as typed")
    args = ap.parse_args()

    raw = args.html
    if not raw:
        try:
            raw = input("HTML file path: ")
        except (EOFError, KeyboardInterrupt):
            sys.exit(1)

    html_path = clean_path(raw).resolve()
    if not html_path.is_file():
        msg = f"File not found: {html_path}"
        if "com.termux" in os.environ.get("PREFIX", ""):
            msg += "\nTermux tip: run `termux-setup-storage`, then use ~/storage/shared/..."
        sys.exit(msg)

    scanner = FieldScanner()
    scanner.feed(read_html(html_path))
    db_path = html_path.with_name(html_path.stem + "_data.json")

    server = None
    for port in range(args.port, args.port + 11):
        try:
            server = ThreadingHTTPServer((args.host, port), Handler)
            break
        except OSError:
            continue
    if server is None:
        sys.exit(f"No free port in {args.port}-{args.port + 10}")

    server.cfg = {"html": html_path, "base": html_path.parent, "db": db_path,
                  "plain": not args.hash_passwords}

    print(f"Detected inputs : {len(scanner.found)}")
    for name, t in scanner.found:
        print(f"   - {name} ({t})")
    print(f"Passwords       : {'clean text (as typed)' if not args.hash_passwords else 'hashed'}")
    print(f"Open in browser : http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{server.server_address[1]}/")
    print(f"Data saved to   : {db_path}")
    print("Stop with Ctrl+C\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
