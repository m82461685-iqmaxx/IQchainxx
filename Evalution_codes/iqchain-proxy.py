#!/usr/bin/env python3
"""
IQchain HTML folder preview and optional Cloudflare Quick Tunnel.

Scans a folder recursively for .html/.htm files, serves them on the first
free 127.0.0.1 port in the configured range, and rotates them in a browser
preview. cloudflared is installed automatically only when tunnel mode is
enabled and it is not already present.

Usage:
    python iqchain-proxy.py
    python iqchain-proxy.py /path/to/html-folder
    python iqchain-proxy.py --folder /path/to/html-folder --no-tunnel

Standard library only on the Python side - no pip install needed.
cloudflared itself is a separate native binary, not a Python package;
this script installs that binary automatically if it's missing.
"""
import argparse
import html
import http.server
import json
import mimetypes
import os
import signal
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
from collections import deque
from pathlib import Path
from urllib.parse import quote, unquote, urlparse, urlsplit
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

GITHUB_RELEASES = "https://github.com/cloudflare/cloudflared/releases/latest/download"
TRYCLOUDFLARE_RE = re.compile(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com")
PRINT_LOCK = threading.Lock()

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

    want_title = avail >= 3
    title_rows = [rule("├", "┤"), line("LIVE ACTIVITY", C.ORANGE, True, True)] if want_title else []
    log_rows = avail - 1 - (2 if want_title else 0)      # one row is the divider

    shown = []                                           # newest at the bottom
    for entry in reversed(_SCREEN["log"]):
        txt = _clean(entry)
        if txt.startswith(("[error]", "[warn]")):
            color = C.ERR
        elif txt.startswith("[saved]"):
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


# ---------------------------------------------------------------------- #
#  BRAND BANNER - IQchain logo, version and developer credit             #
# ---------------------------------------------------------------------- #
APP_NAME = "IQchain"
APP_VERSION = "v0.01"
APP_AUTHOR = "IQmaxx (DK)"
APP_TAG = "(tunnel)"

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
    """Logo, version, the '(tunnel)' tag and the developer credit, all
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
    lines.append(row("(tunnel)", color=C.ORANGE, bold=True))
    lines.append(row(f"developed by {APP_AUTHOR}", color=C.GREY))
    lines.append(blank())
    lines.append(f"{C.ORANGE}└{'─' * (w - 2)}┘{C.RESET}")

    print()
    sys.stdout.write("\n".join(lines) + "\n")


def print_farewell(outcome):
    """Printed on the REAL terminal, right below the command the user ran,
    after the full-screen app has closed: logo + (tunnel) tag + developer
    credit, then a closing status box and a goodbye line."""
    print_banner()
    print()
    if outcome == "ended":
        print_box(["cloudflared stopped on its own - the tunnel is no longer live"],
                  title="TUNNEL ENDED", color=C.ERR)
    else:
        print_box(["tunnel closed - the public link no longer works"],
                  title="STOPPED", color=C.GREY)
    sys.stdout.write(f"\n{C.BOLD}{C.ORANGE}  Thanks for using {APP_NAME} - come again!{C.RESET}\n")
    sys.stdout.write(f"{C.GREY}  developed by {APP_AUTHOR}{C.RESET}\n\n")
    sys.stdout.flush()


def step_header(n, total, title):
    label = f"STEP {n}/{total}  -  {title.upper()}"
    print()
    sys.stdout.write(f"{C.BOLD}{C.ORANGE}{label}{C.RESET}\n")
    sys.stdout.write(f"{C.GREY}{'-' * min(len(label), _term_width())}{C.RESET}\n")


def ask_choice(message, options):
    """The single point every failure in this program goes through: one
    clear sentence explaining what went wrong, in a bordered box, with a
    short list of lettered options - never a bare traceback. Returns the
    lowercase key the user picked."""
    opt_line = "   ".join(f"[{k.upper()}] {label}" for k, label in options)
    print_box([message, "", opt_line], title="WHAT WOULD YOU LIKE TO DO?", color=C.ORANGE)
    valid = {k.lower() for k, _ in options}
    while True:
        choice = safe_input(f"{C.GREY}> {C.RESET}").strip().lower()
        if choice in valid:
            return choice
        sys.stdout.write(f"{C.ERR}  please pick one of: {', '.join(k.upper() for k, _ in options)}{C.RESET}\n")


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


class Spinner:
    """A fast inline spinner on the current terminal line. Animates
    unconditionally and falls back gracefully if writing fails."""
    FRAMES = ["*" * i for i in range(1, 9)]

    def __init__(self, prompt):
        self.prompt = prompt
        self._stop = threading.Event()
        self._thread = None
        self._printed = 0

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


# ====================================================================== #
#  CLOUDFLARED: find / install                                           #
# ====================================================================== #
def _install_dir():
    d = Path.home() / ".local" / "bin"
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_cloudflared():
    """Return a runnable path to cloudflared, or None."""
    exe = shutil.which("cloudflared")
    if exe:
        return exe
    for name in ("cloudflared", "cloudflared.exe"):
        candidate = _install_dir() / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _is_termux():
    return bool(os.environ.get("PREFIX", "").find("com.termux") != -1 or Path("/data/data/com.termux").exists())


def _asset_for_platform():
    """Maps this machine to the matching asset name on Cloudflare's
    GitHub releases page. Returns (asset_name, kind) where kind tells the
    installer whether the download is a raw binary or a .tgz archive."""
    system = platform.system()
    machine = platform.machine().lower()

    if system == "Linux":
        arch_map = {
            "x86_64": "amd64", "amd64": "amd64",
            "aarch64": "arm64", "arm64": "arm64",
            "armv7l": "arm", "armv8l": "arm",
            "i686": "386", "i386": "386",
        }
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
    """Tries the fastest, most native route first (Termux's pkg, or
    Homebrew on macOS), then falls back to downloading the official
    binary straight from Cloudflare's GitHub releases - that fallback
    works on any Linux, macOS or Windows machine with no package
    manager assumptions at all."""
    if _is_termux() and shutil.which("pkg"):
        with Spinner("installing cloudflared via pkg ") as sp:
            res = subprocess.run(["pkg", "install", "-y", "cloudflared"],
                                  capture_output=True, text=True)
        sp.finish("")
        found = find_cloudflared()
        if res.returncode == 0 and found:
            return found, None

    elif platform.system() == "Darwin" and shutil.which("brew"):
        with Spinner("installing cloudflared via brew ") as sp:
            res = subprocess.run(["brew", "install", "cloudflared"],
                                  capture_output=True, text=True)
        sp.finish("")
        found = find_cloudflared()
        if res.returncode == 0 and found:
            return found, None

    with Spinner("downloading cloudflared ") as sp:
        path, err = _download_binary()
    sp.finish("")
    return path, err


# ====================================================================== #
#  LOCAL URL                                                              #
# ====================================================================== #
def _reachable(url):
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except Exception:
        return False


def ask_local_url(cli_value):
    step_header(2, 3, "Enter the local address to expose")
    raw = cli_value
    while True:
        if not raw:
            raw = safe_input(f"{C.WHITE}Local URL (e.g. localhost:3000): {C.RESET}").strip()
        candidate = raw
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", candidate):
            candidate = "http://" + candidate
        parsed = urlparse(candidate)
        if not parsed.hostname:
            choice = ask_choice(
                "that does not look like a valid local address, such as localhost:3000",
                [("r", "try again"), ("q", "quit")],
            )
            if choice == "q":
                sys.exit(0)
            raw = None
            continue

        ok_line(f"using local address: {candidate}")
        with Spinner("checking that it is reachable ") as sp:
            reachable = _reachable(candidate)
        sp.finish("")
        if not reachable:
            print_box(
                [f"nothing answered at {candidate} just now - that is fine if the",
                 "local server has not been started yet, the tunnel can wait for it"],
                title="HEADS UP", color=C.GREY,
            )
        return candidate


# ====================================================================== #
#  HTML FOLDER PREVIEW                                                    #
# ====================================================================== #
def scan_html_files(folder):
    """Find every regular .html/.htm file below folder, in stable order."""
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"not a folder: {root}")
    files = []
    for item in root.rglob("*"):
        if item.suffix.lower() not in (".html", ".htm") or not item.is_file():
            continue
        resolved = item.resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            continue                         # do not serve symlinks outside the folder
        files.append((relative.as_posix(), resolved))
    files.sort(key=lambda item: item[0].casefold())
    if not files:
        raise ValueError("no .html or .htm files were found in that folder")
    return root, files


def ask_html_folder(cli_folder):
    step_header(1, 3, "Choose the folder containing HTML files")
    raw = cli_folder
    while True:
        if not raw:
            raw = safe_input(f"{C.WHITE}Folder path: {C.RESET}").strip()
        raw = raw.strip().strip('"').strip("'")
        try:
            root, files = scan_html_files(raw)
            ok_line(f"found {len(files)} HTML file(s) under {root}")
            _SCREEN["log"].append(f"[saved] scanned {len(files)} HTML file(s)")
            return root, files
        except (OSError, ValueError) as exc:
            choice = ask_choice(str(exc), [("r", "enter another folder"), ("q", "quit")])
            if choice == "q":
                sys.exit(0)
            raw = None


def make_preview_handler(root, files, interval):
    """Build a local-only static handler with a fullscreen rotating index."""
    routes = ["/__iqchain/site/" + quote(rel, safe="/") for rel, _path in files]
    route_json = json.dumps(routes, ensure_ascii=True).replace("<", "\\u003c")
    title = html.escape(root.name or str(root))
    page_template = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>IQchain HTML preview</title><style>
*{box-sizing:border-box}html,body{height:100%;margin:0;background:#111;color:#f4f4f4;font:14px system-ui,sans-serif}
body{display:flex;flex-direction:column}.bar{min-height:42px;padding:7px 10px;display:flex;align-items:center;gap:8px;background:#202020;border-bottom:1px solid #444}
.name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.bar button{background:#303030;color:#fff;border:1px solid #777;border-radius:5px;padding:6px 10px;font:inherit}
.bar button:active{background:#555}#frame{width:100%;height:100%;border:0;background:white;flex:1}#count{white-space:nowrap;color:#bbb}
</style></head><body><header class="bar"><span class="name" id="name">__TITLE__</span><span id="count"></span>
<button id="prev" type="button" aria-label="Previous">‹</button><button id="toggle" type="button">Pause</button><button id="next" type="button" aria-label="Next">›</button></header>
<iframe id="frame" title="HTML preview" allow="fullscreen" referrerpolicy="no-referrer"></iframe>
<script>
const sites=__ROUTES__, delay=__INTERVAL__*1000;let index=0,paused=false;
const frame=document.getElementById('frame'),count=document.getElementById('count'),name=document.getElementById('name'),toggle=document.getElementById('toggle');
function show(i){index=(i+sites.length)%sites.length;frame.src=sites[index];count.textContent=(index+1)+' / '+sites.length;name.title=decodeURIComponent(sites[index].replace('/__iqchain/site/',''));}
document.getElementById('prev').onclick=()=>show(index-1);document.getElementById('next').onclick=()=>show(index+1);
toggle.onclick=()=>{paused=!paused;toggle.textContent=paused?'Resume':'Pause';};
window.addEventListener('keydown',e=>{if(e.key==='ArrowRight')show(index+1);if(e.key==='ArrowLeft')show(index-1);if(e.code==='Space'){e.preventDefault();toggle.click();}});
show(0);if(sites.length>1)setInterval(()=>{if(!paused)show(index+1);},delay);
</script></body></html>"""
    page = (page_template.replace("__TITLE__", title)
            .replace("__ROUTES__", route_json)
            .replace("__INTERVAL__", str(float(interval))))

    class PreviewHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path in ("/", "/index.html"):
                body = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if path.startswith("/__iqchain/site/"):
                relative = unquote(path[len("/__iqchain/site/"):])
                candidate = (root / relative).resolve()
                try:
                    candidate.relative_to(root)
                except ValueError:
                    self.send_error(404)
                    return
                if not candidate.is_file():
                    self.send_error(404)
                    return
                self.path = "/" + quote(candidate.relative_to(root).as_posix(), safe="/")
            super().do_GET()

        def do_HEAD(self):
            if urlsplit(self.path).path in ("/", "/index.html"):
                body = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            super().do_HEAD()

        def list_directory(self, path):
            self.send_error(404, "Directory listing is disabled")
            return None

        def log_message(self, fmt, *args):
            pass                              # avoid flooding the live dashboard

    return PreviewHandler


def start_preview_server(root, files, start_port=8000, end_port=8300, interval=5):
    if not (1 <= start_port <= end_port <= 65535):
        raise ValueError("port range must satisfy 1 <= --port <= --port-end <= 65535")
    handler = make_preview_handler(root, files, interval)
    tried = list(range(start_port, end_port + 1))
    for port in tried:
        try:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            return server, port
        except OSError:
            continue
    raise OSError(f"no free localhost port found between {start_port} and {end_port}")


# ====================================================================== #
#  TUNNEL                                                                 #
# ====================================================================== #
def run_tunnel(cloudflared_path, local_url, extra_header=None):
    step_header(3, 3, "Open the Cloudflare tunnel")
    cmd = [cloudflared_path, "tunnel", "--url", local_url]

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except Exception as e:
        ask_choice(f"could not start cloudflared - {e}", [("q", "quit")])
        sys.exit(0)

    state = {"public_url": None, "lines": []}
    found = threading.Event()

    def reader():
        for line in proc.stdout:
            text = line.rstrip()
            state["lines"].append(text)
            if _SCREEN["active"]:
                live_box_line(text)
            else:
                _SCREEN["log"].append(_clean(text))
            m = TRYCLOUDFLARE_RE.search(line)
            if m and not state["public_url"]:
                state["public_url"] = m.group(0)
                found.set()

    threading.Thread(target=reader, daemon=True).start()

    try:
        with Spinner("opening tunnel ") as sp:
            got_link = found.wait(timeout=30)
        sp.finish("")
    except KeyboardInterrupt:
        proc.terminate()
        return "stopped"

    if not got_link:
        proc.terminate()
        tail = state["lines"][-6:] or ["(cloudflared produced no output)"]
        print_box(tail, title="CLOUDFLARED OUTPUT", color=C.GREY)
        choice = ask_choice(
            "cloudflared did not hand back a public link within 30 seconds",
            [("r", "try again"), ("q", "quit")],
        )
        if choice == "r":
            return run_tunnel(cloudflared_path, local_url, extra_header=extra_header)
        sys.exit(0)

    print()
    print_box(
        [
            f"Local:  {local_url}",
            f"Public: {state['public_url']}",
            *(extra_header or []),
            "",
            "This link works from anywhere while this window stays open.",
            "Anyone with the public link can access the files being served.",
            "Switching to full-screen live view ...",
        ],
        title="TUNNEL IS LIVE", color=C.ORANGE,
    )

    # the step-by-step history above disappears here - only the live
    # dashboard (this function's own output) stays on screen from now
    set_dashboard_header([
        f"Local:  {local_url}",
        f"Public: {state['public_url']}",
        *(extra_header or []),
        "This link works from anywhere while this window stays open.",
        "Press Ctrl+C here to stop the tunnel.",
    ])
    start_dashboard()
    try:
        proc.wait()
        return "ended"
    except KeyboardInterrupt:
        proc.terminate()
        return "stopped"


# ====================================================================== #
#  MAIN                                                                   #
# ====================================================================== #
def main():
    setup_console()
    ap = argparse.ArgumentParser(description="Rotate through HTML files from a folder on localhost, with an optional Quick Tunnel")
    ap.add_argument("folder_path", nargs="?", help="folder containing .html/.htm files")
    ap.add_argument("--folder", dest="folder_option", help="folder containing .html/.htm files")
    ap.add_argument("--port", type=int, default=8000, help="first localhost port to try (default: 8000)")
    ap.add_argument("--port-end", type=int, default=8300, help="last fallback port (default: 8300)")
    ap.add_argument("--interval", type=float, default=2, help="seconds per HTML page; minimum 1 (default: 2)")
    ap.add_argument("--no-tunnel", action="store_true", help="serve locally only; do not install or start cloudflared")
    args = ap.parse_args()
    if not (1 <= args.interval <= 3600):
        ap.error("--interval must be between 1 and 3600 seconds")

    # ---- the whole program, from this line to the very end, runs inside
    # the alternate screen buffer: the instant it activates it takes over
    # the full terminal/tab like a standalone application - whatever was
    # already on screen (earlier commands, scrollback) is hidden for the
    # entire run and reappears untouched only after this exits. ----------
    enter_app_screen()
    outcome = None
    error_message = None
    server = None
    try:
        print_banner()
        root, files = ask_html_folder(args.folder_option or args.folder_path)

        step_header(2, 3, "Start the rotating localhost preview")
        try:
            server, port = start_preview_server(
                root, files, start_port=args.port, end_port=args.port_end,
                interval=args.interval,
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"could not start the local preview server: {exc}")
        local_url = f"http://127.0.0.1:{port}"
        ok_line(f"serving {len(files)} HTML file(s) at {local_url}")
        ok_line(f"automatic page rotation: every {args.interval:g} second(s)")
        if port != args.port:
            ok_line(f"port {args.port} was busy; selected the next available port {port}")

        extra_header = [
            f"Folder: {root}",
            f"HTML files: {len(files)} | rotation: {args.interval:g}s",
            "Use the preview controls or left/right arrow keys to browse.",
        ]
        if args.no_tunnel:
            set_dashboard_header([f"Local: {local_url}", *extra_header,
                                  "Local-only mode; press Ctrl+C to stop."])
            start_dashboard()
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                outcome = "stopped"
        else:
            step_header(3, 3, "Check for cloudflared")
            path = find_cloudflared()
            if path:
                ok_line(f"cloudflared is already installed: {path}")
            else:
                print_box(["cloudflared was not found on this device - installing it now"],
                           title="INSTALLING", color=C.GREY)
                path, err = install_cloudflared()
                while not path:
                    choice = ask_choice(
                        f"automatic installation did not succeed - {err or 'unknown error'}",
                        [("r", "try again"), ("q", "quit")],
                    )
                    if choice == "q":
                        sys.exit(0)
                    path, err = install_cloudflared()
                ok_line(f"cloudflared installed: {path}")
            outcome = run_tunnel(path, local_url, extra_header=extra_header)
    except KeyboardInterrupt:      # Ctrl+C at ANY step still shows the farewell
        outcome = outcome or "stopped"
    except Exception as exc:
        error_message = str(exc)
        outcome = outcome or "stopped"
    except SystemExit:             # sys.exit() inside a step (quit choice) too
        outcome = outcome or "stopped"
    finally:
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
        leave_app_screen()   # restores the user's real terminal, untouched

    try:
        if error_message:
            print_box([error_message], title="ERROR", color=C.ERR)
        print_farewell(outcome)   # logo + (tunnel) + developer credit + come again
    except KeyboardInterrupt:
        pass
    sys.exit(0)   # the application has fully exited back to the command line


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        sys.exit(0)
