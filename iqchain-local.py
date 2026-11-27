#!/usr/bin/env python3
"""
iqchain-local.py

Shares ONE server running on 127.0.0.1:<port> (any port, 8000-8300 by default)
with other devices on the SAME Wi-Fi / hotspot. No Cloudflare, no internet
tunnel, nothing to install.

Why a friend cannot open "127.0.0.1:8000": on his device that address means
HIS OWN device. He must use THIS device's network address (e.g.
192.168.43.1:8000), and the server has to accept network connections. This
script finds this device's address, finds your running server, opens a relay
if the server is loopback-only, and prints the exact address to share.

Usage:
    python iqchain-local.py                    # scan 8000-8300, pick one
    python iqchain-local.py 8000               # this port
    python iqchain-local.py 127.0.0.1:8000     # same thing
    python iqchain-local.py --rewrite-host     # for servers that reject unknown Host

Standard library only - no pip install needed.
"""
import argparse
import os
import signal
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlparse
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
APP_VERSION = "v4.00"
APP_AUTHOR = "IQmaxx (DK)"
APP_TAG = "(local)"

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
    lines.append(row("(local)", color=C.ORANGE, bold=True))
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
        print_box(["the relay stopped on its own - nothing is shared any more"],
                  title="SHARING ENDED", color=C.ERR)
    else:
        print_box(["sharing stopped - the address no longer works"],
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
#  LOCAL-NETWORK SHARING LOGIC                                           #
#                                                                        #
#  The problem this solves: a server started on 127.0.0.1 can only be    #
#  reached from the SAME device. "127.0.0.1" / "localhost" on a friend's #
#  phone means the friend's own phone, so it never works for them.       #
#  Two things are needed for a second device to reach it:                #
#    1. the friend must use THIS device's network address               #
#       (e.g. 192.168.43.1), not 127.0.0.1                               #
#    2. the server must accept connections from the network. A server    #
#       bound to 127.0.0.1 refuses them - so this program opens a small  #
#       relay on 0.0.0.0 that forwards everything to the loopback server #
#  No internet tunnel is used - this works on the local network only.   #
# ====================================================================== #
PORT_LO, PORT_HI = 8000, 8300
LOOPBACK = "127.0.0.1"

_HTTP_METHODS = (b"GET", b"POST", b"PUT", b"DELETE", b"HEAD", b"OPTIONS", b"PATCH")
_HOST_HDR_RE = re.compile(rb"(?im)^host:[ \t]*[^\r\n]*")

CLIENTS = {}                       # visitor ip -> {"first","last","hits","ports"}
CLIENTS_LOCK = threading.Lock()


# ---------------------------------------------------------------------- #
#  Network address detection                                             #
# ---------------------------------------------------------------------- #
def _is_usable_ip(ip):
    try:
        parts = [int(p) for p in str(ip).split(".")]
    except ValueError:
        return False
    if len(parts) != 4 or any(p < 0 or p > 255 for p in parts):
        return False
    if parts[0] in (0, 127) or parts[0] >= 224:
        return False
    if parts[0] == 169 and parts[1] == 254:          # link-local, not routable
        return False
    return True


def _is_private_ip(ip):
    a, b = (int(x) for x in ip.split(".")[:2])
    return a == 10 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31)


def _guess_label(ip, label):
    if ip.startswith(("192.168.43.", "172.20.10.", "192.168.137.")):
        return f"{label}, hotspot range"
    return label


def _run_quiet(cmd):
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=4)
        return res.stdout if res.returncode == 0 else ""
    except Exception:
        return ""


def detect_lan_ips():
    """Every usable IPv4 address of this device as [(ip, label)], the
    best guess first. Uses only the standard library plus the system's own
    tools (ip / ifconfig / ipconfig), and psutil when it happens to exist.
    Every method is optional - one failing never stops the others."""
    found = []

    def add(ip, label):
        if _is_usable_ip(ip) and ip not in [f[0] for f in found]:
            found.append((ip, _guess_label(ip, label)))

    # 1) the address the OS would use to reach the outside (no packet is sent)
    for target in ("10.255.255.255", "192.168.255.255", "8.8.8.8"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(1)
            s.connect((target, 1))
            add(s.getsockname()[0], "main connection")
        except Exception:
            pass
        finally:
            s.close()

    # 2) psutil, if installed
    try:
        import psutil
        for name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family == socket.AF_INET:
                    add(a.address, name)
    except Exception:
        pass

    # 3) system tools - this is what finds a hotspot interface that has
    #    no internet route (the UDP trick above cannot see those)
    out = _run_quiet(["ip", "-4", "-o", "addr", "show"])
    for m in re.finditer(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)", out, re.M):
        add(m.group(2), m.group(1))
    if not out:
        out = _run_quiet(["ifconfig"])
        iface = "interface"
        for line in out.splitlines():
            head = re.match(r"^(\S+?):?\s", line)
            if head and not line.startswith((" ", "\t")):
                iface = head.group(1)
            m = re.search(r"inet\s+(?:addr:)?(\d+\.\d+\.\d+\.\d+)", line)
            if m:
                add(m.group(1), iface)
    if os.name == "nt":
        out = _run_quiet(["ipconfig"])
        for m in re.finditer(r"IPv4[^:\n]*:\s*(\d+\.\d+\.\d+\.\d+)", out):
            add(m.group(1), "adapter")

    # 4) hostname lookup
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            add(ip, "hostname")
    except Exception:
        pass

    found.sort(key=lambda f: 0 if _is_private_ip(f[0]) else 1)   # stable sort
    return found


# ---------------------------------------------------------------------- #
#  Port scanning / probing                                               #
# ---------------------------------------------------------------------- #
def expand_ports(values):
    """CLI values -> list of ports to scan. Nothing given = 8000-8300.
    Accepts '8000', '8000 8010' and '8000-8100'."""
    if not values:
        return list(range(PORT_LO, PORT_HI + 1))
    ports = []
    for v in values:
        v = v.strip().replace("127.0.0.1:", "").replace("localhost:", "")
        m = re.fullmatch(r"(\d{1,5})-(\d{1,5})", v)
        if m:
            lo, hi = sorted((int(m.group(1)), int(m.group(2))))
            ports.extend(range(lo, min(hi, 65535) + 1))
        elif v.isdigit() and 0 < int(v) < 65536:
            ports.append(int(v))
    return sorted(set(ports)) or list(range(PORT_LO, PORT_HI + 1))


def port_is_open(port, host=LOOPBACK, timeout=0.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def scan_ports(ports, host=LOOPBACK, workers=64):
    pending = list(ports)
    open_ports = []
    lock = threading.Lock()

    def worker():
        while True:
            with lock:
                if not pending:
                    return
                p = pending.pop()
            if port_is_open(p, host, 0.4):
                with lock:
                    open_ports.append(p)

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(min(workers, max(len(pending), 1)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sorted(open_ports)


def probe_service(port):
    """One short line describing what is listening on this port."""
    data = b""
    try:
        with socket.create_connection((LOOPBACK, port), timeout=1.0) as s:
            s.settimeout(1.0)
            s.sendall(b"GET / HTTP/1.0\r\nHost: localhost\r\nUser-Agent: iqchain\r\n\r\n")
            try:
                while len(data) < 4096:
                    chunk = s.recv(2048)
                    if not chunk:
                        break
                    data += chunk
            except socket.timeout:
                pass
    except Exception:
        return "not answering"
    if data.startswith(b"HTTP/"):
        status = data.split(b"\r\n", 1)[0].decode("latin-1")[9:12]
        m = re.search(rb"<title[^>]*>(.*?)</title>", data, re.I | re.S)
        title = m.group(1).decode("utf-8", "replace").strip()[:28] if m else ""
        return f"HTTP {status}" + (f" - {title}" if title else "")
    return "TCP service" if data else "TCP service (silent)"


def exposed_on_network(ips, port):
    """True when the server already accepts connections on one of this
    device's network addresses (it was started on 0.0.0.0 / that address).
    False means it is loopback-only and needs the relay."""
    return any(port_is_open(port, ip, 1.0) for ip, _ in ips)


# ---------------------------------------------------------------------- #
#  Relay: network address -> loopback server                             #
# ---------------------------------------------------------------------- #
class Relay:
    """A plain TCP relay listening on 0.0.0.0 and forwarding every
    connection to 127.0.0.1:<target>. TCP-level, so it carries HTTP,
    WebSockets, SSE and anything else unchanged. Optionally rewrites the
    HTTP Host header to 127.0.0.1:<target> for dev servers that reject
    unknown hosts (Django ALLOWED_HOSTS, Vite, webpack ...)."""

    def __init__(self, target_port, listen_port, rewrite_host=False):
        self.target = target_port
        self.listen = listen_port
        self.rewrite = rewrite_host
        self.sock = None
        self.stopped = threading.Event()

    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":                      # on Windows this flag allows port hijacking
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", self.listen))
            s.listen(128)
        except Exception:
            s.close()
            raise
        self.sock = s
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def stop(self):
        self.stopped.set()
        try:
            self.sock.close()
        except Exception:
            pass

    def alive(self):
        return not self.stopped.is_set()

    def _accept_loop(self):
        while not self.stopped.is_set():
            try:
                conn, addr = self.sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn, addr[0]), daemon=True).start()
        self.stopped.set()

    def _note_client(self, ip):
        now = time.time()
        with CLIENTS_LOCK:
            c = CLIENTS.get(ip)
            new = c is None
            if new:
                c = CLIENTS[ip] = {"first": now, "last": now, "hits": 0, "ports": set()}
            c["last"] = now
            c["hits"] += 1
            c["ports"].add(self.target)
        if new:
            live_box_line(f"[new] device {ip} is now using :{self.target}")

    def _handle(self, conn, ip):
        try:
            up = socket.create_connection((LOOPBACK, self.target), timeout=5)
        except Exception as e:
            live_box_line(f"[warn] {ip} -> :{self.target} but the local server is not answering ({e.__class__.__name__})")
            conn.close()
            return
        conn.settimeout(None)
        up.settimeout(None)
        self._note_client(ip)
        host_value = b"Host: 127.0.0.1:" + str(self.target).encode()

        def pipe(src, dst, client_side):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    if client_side and self.rewrite and data.split(b" ", 1)[0] in _HTTP_METHODS:
                        data = _HOST_HDR_RE.sub(host_value, data, count=1)
                    dst.sendall(data)
            except Exception:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except Exception:
                    pass

        t = threading.Thread(target=pipe, args=(conn, up, True), daemon=True)
        t.start()
        pipe(up, conn, False)
        t.join(timeout=30)
        for s in (conn, up):
            try:
                s.close()
            except Exception:
                pass


def start_relay(target_port, rewrite_host):
    """Prefers the SAME port number as the real server, so the friend uses
    the same number. Linux refuses that while 127.0.0.1:<port> is taken, so
    it falls back to <port>+1000 (8000 -> 9000) and upwards."""
    candidates = [target_port] + list(range(target_port + 1000, target_port + 1100))
    last_err = None
    for lp in candidates:
        if lp > 65535:
            break
        relay = Relay(target_port, lp, rewrite_host)
        try:
            relay.start()
            return relay, None
        except Exception as e:
            last_err = e
    return None, str(last_err)


# ---------------------------------------------------------------------- #
#  Session state                                                         #
# ---------------------------------------------------------------------- #
class Share:
    def __init__(self, target_port, listen_port, mode, relay=None):
        self.target = target_port          # the real local server port
        self.listen = listen_port          # the port other devices must use
        self.mode = mode                   # "direct" or "relay"
        self.relay = relay
        self.up = True                     # is the local server answering?


class Session:
    def __init__(self):
        self.ips = []
        self.shares = []
        self.rewrite = False

    def close(self):
        for sh in self.shares:
            if sh.relay:
                sh.relay.stop()


def lan_urls(session, share, limit=3):
    return [f"{ip}:{share.listen}" for ip, _ in session.ips[:limit]]


def build_header(session):
    lines = []
    for sh in session.shares:
        how = "already open to the network" if sh.mode == "direct" else f"relay :{sh.listen} -> 127.0.0.1:{sh.target}"
        state = "" if sh.up else "   [local server NOT answering]"
        lines.append(f"Local   127.0.0.1:{sh.target}   ({how}){state}")
        for url in lan_urls(session, sh):
            lines.append(f"Share   {url}")
    lines.append("Friend opens the Share address in a browser, same Wi-Fi / hotspot.")
    with CLIENTS_LOCK:
        if CLIENTS:
            who = ", ".join(sorted(CLIENTS))
            lines.append(f"Devices seen: {len(CLIENTS)}  ({who})")
        else:
            lines.append("Devices seen: none yet")
    lines.append("Press Ctrl+C here to stop sharing.")
    return lines


# ---------------------------------------------------------------------- #
#  Steps                                                                 #
# ---------------------------------------------------------------------- #
def step_network():
    step_header(1, 3, "Find this device's network address")
    while True:
        with Spinner("looking at network interfaces ") as sp:
            ips = detect_lan_ips()
        sp.finish("")
        if ips:
            break
        choice = ask_choice(
            "no network address found - is Wi-Fi or the hotspot connected? "
            "You can also type this device's address yourself",
            [("r", "scan again"), ("m", "type it"), ("q", "quit")],
        )
        if choice == "q":
            sys.exit(0)
        if choice == "m":
            raw = safe_input(f"{C.WHITE}This device's IP (e.g. 192.168.43.1): {C.RESET}").strip()
            if _is_usable_ip(raw):
                ips = [(raw, "typed by you")]
                break
            sys.stdout.write(f"{C.ERR}  that is not a valid IPv4 address{C.RESET}\n")
    for ip, label in ips[:4]:
        ok_line(f"{ip}   ({label})")
    return ips


def step_scan(port_args):
    step_header(2, 3, "Find the local server")
    ports = expand_ports(port_args)
    span = f"{ports[0]}-{ports[-1]}" if len(ports) > 1 else str(ports[0])
    while True:
        with Spinner(f"scanning 127.0.0.1 ports {span} ") as sp:
            found = scan_ports(ports)
        sp.finish("")
        if found:
            break
        choice = ask_choice(
            f"nothing answered on 127.0.0.1 ports {span} - start your local server first",
            [("r", "scan again"), ("m", "type a port"), ("q", "quit")],
        )
        if choice == "q":
            sys.exit(0)
        if choice == "m":
            raw = safe_input(f"{C.WHITE}Port number: {C.RESET}").strip()
            if raw.isdigit() and 0 < int(raw) < 65536:
                return [int(raw)]
            sys.stdout.write(f"{C.ERR}  that is not a valid port{C.RESET}\n")
            continue
        ports = expand_ports(port_args)

    rows = [f"[{i}]  127.0.0.1:{p}   {probe_service(p)}" for i, p in enumerate(found, 1)]
    print_box(rows, title="LOCAL SERVERS FOUND", color=C.GREY)

    if len(found) == 1:
        ok_line(f"using 127.0.0.1:{found[0]}")
        return found
    while True:
        raw = safe_input(f"{C.WHITE}Share which one? type its number: {C.RESET}").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(found):
            ok_line(f"using 127.0.0.1:{found[int(raw) - 1]}")
            return [found[int(raw) - 1]]
        sys.stdout.write(f"{C.ERR}  please type one number from the list{C.RESET}\n")


def step_share(session, selected):
    step_header(3, 3, "Open the server to the network")
    for port in selected:
        if exposed_on_network(session.ips, port):
            session.shares.append(Share(port, port, "direct"))
            ok_line(f":{port} already accepts network connections - no relay needed")
            continue
        relay, err = start_relay(port, session.rewrite)
        if not relay:
            print_box([f"could not open a relay for :{port} - {err}"],
                      title="RELAY FAILED", color=C.ERR)
            continue
        session.shares.append(Share(port, relay.listen, "relay", relay))
        ok_line(f":{port} was loopback-only - relay opened on :{relay.listen}")
        if not port_is_open(relay.listen, session.ips[0][0], 1.5):
            print_box([f"this device cannot even reach itself on {session.ips[0][0]}:{relay.listen}",
                       "a firewall is probably blocking it - allow Python / this port",
                       "in the firewall, then run again"],
                      title="HEADS UP", color=C.GREY)
    if not session.shares:
        ask_choice("nothing could be shared", [("q", "quit")])
        sys.exit(0)

    lines = []
    for sh in session.shares:
        lines.append(f"Local 127.0.0.1:{sh.target}  - tell your friend to open:")
        lines.extend(f"   {u}" for u in lan_urls(session, sh))
        lines.append("")
    lines += [
        "Your friend must be on the SAME Wi-Fi / hotspot and must use the Share address above - never 127.0.0.1 or localhost (that means his own device).",
        "",
        "Still not opening?  (1) type http:// in front of the address, not https://   (2) some hotspots block device-to-device traffic (client / AP isolation) - turn that off, or use another hotspot   (3) allow Python in the firewall.",
    ]
    print()
    print_box(lines, title="SHARED ON THE LOCAL NETWORK", color=C.ORANGE)


# ---------------------------------------------------------------------- #
#  Live loop                                                             #
# ---------------------------------------------------------------------- #
def run_live(session):
    lines = []
    for sh in session.shares:
        lines.append(f"Local 127.0.0.1:{sh.target}")
        lines.extend(f"Share {u}" for u in lan_urls(session, sh))
    print()
    print_box(lines + ["", "Switching to full-screen live view ..."],
              title="SHARING IS LIVE", color=C.ORANGE)

    set_dashboard_header(build_header(session))
    start_dashboard()
    last_ip_check = last_srv_check = time.time()
    try:
        while True:
            time.sleep(1)
            now = time.time()

            if now - last_ip_check >= 8:                 # hotspot / Wi-Fi may change
                last_ip_check = now
                fresh = detect_lan_ips()
                if fresh and [i for i, _ in fresh] != [i for i, _ in session.ips]:
                    session.ips = fresh
                    live_box_line(f"[warn] network address changed - friend must use {fresh[0][0]} now")

            if now - last_srv_check >= 5:                # is the real server still up?
                last_srv_check = now
                for sh in session.shares:
                    alive = port_is_open(sh.target)
                    if alive != sh.up:
                        sh.up = alive
                        live_box_line(f"[{'ok' if alive else 'error'}] local server :{sh.target} is "
                                      f"{'answering again' if alive else 'not answering'}")

            for sh in session.shares:
                if sh.relay and not sh.relay.alive():
                    live_box_line(f"[error] relay on :{sh.listen} stopped")
                    return "ended"

            set_dashboard_header(build_header(session))
    except KeyboardInterrupt:
        return "stopped"


# ====================================================================== #
#  MAIN                                                                   #
# ====================================================================== #
def main():
    setup_console()
    ap = argparse.ArgumentParser(
        description="Share ONE local server (127.0.0.1:<port>) with other devices on the same Wi-Fi / hotspot")
    ap.add_argument("ports", nargs="*",
                    help="one port, e.g. 8000  or  127.0.0.1:8000  (default: look at 8000-8300)")
    ap.add_argument("--rewrite-host", action="store_true",
                    help="rewrite the HTTP Host header to 127.0.0.1:<port> "
                         "(for dev servers that reject unknown hosts)")
    args = ap.parse_args()

    enter_app_screen()
    outcome = None
    session = Session()
    session.rewrite = args.rewrite_host
    try:
        print_banner()
        session.ips = step_network()
        selected = step_scan(args.ports)
        step_share(session, selected)
        outcome = run_live(session)
    except KeyboardInterrupt:
        outcome = outcome or "stopped"
    except SystemExit:
        outcome = outcome or "stopped"
    finally:
        session.close()
        leave_app_screen()

    try:
        print_farewell(outcome)
    except KeyboardInterrupt:
        pass
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        sys.exit(0)
