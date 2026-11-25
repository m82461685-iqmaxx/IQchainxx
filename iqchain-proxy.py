#!/usr/bin/env python3
"""IQchain local HTML preview.

Scans a selected folder recursively, serves it ONLY on 127.0.0.1, and
continuously rotates through HTML files in a browser preview. Standard
library only: no cloud tunnel, public URL, external install, or dependency.
"""
import argparse
import atexit
import codecs
import http.server
import json
import mimetypes
import os
import re
import shutil
import signal
import socket
import sys
import threading
import time
import unicodedata
from collections import deque
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

try:
    import termios
except ImportError:
    termios = None
try:
    import msvcrt
except ImportError:
    msvcrt = None

PRINT_LOCK = threading.Lock()
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-_]")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SCREEN = {
    "active": False,
    "alt": False,
    "size": None,
    "header": [],
    "log": deque(maxlen=1000),
}

# Alternate-screen escape codes - the same trick full-screen terminal apps
# like htop, vim and less use. Switching to the alternate buffer makes this
# program take over the whole tab/window like a real application; leaving
# it instantly restores the user's previous terminal content untouched.
_ALT_SCREEN_ON = "\033[?1049h\033[2J\033[H"                 # switch buffer + clear + home
_ALT_SCREEN_OFF = "\033[0m\033[?7h\033[?25h\033[?1049l"     # reset + wrap on + cursor on + switch back

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
    drawn. Incoming request text can therefore never move the cursor or
    break the frame."""
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
    """Records one line of local activity. While the full-screen view is active the frame is re-drawn so
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
APP_VERSION = "V4.00"
APP_AUTHOR = "IQmaxx (DK)"
APP_TAG = "(Proxy)"

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
    """Logo, version, the product tag and the developer credit, all
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
    lines.append(row(APP_TAG, color=C.ORANGE, bold=True))
    lines.append(row(f"developed by {APP_AUTHOR}", color=C.GREY))
    lines.append(blank())
    lines.append(f"{C.ORANGE}└{'─' * (w - 2)}┘{C.RESET}")

    print()
    sys.stdout.write("\n".join(lines) + "\n")


def print_farewell(outcome):
    """Printed on the REAL terminal, right below the command the user ran,
    after the full-screen app has closed: logo + original tag + developer
    credit, then a closing status box and a goodbye line."""
    print_banner()
    print()
    if outcome == "ended":
        print_box(["the local preview server stopped"],
                  title="PREVIEW ENDED", color=C.ERR)
    else:
        print_box(["local preview stopped - the localhost page is no longer available"],
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
#  LOCAL HTML PREVIEW                                                     #
# ====================================================================== #
def scan_html_files(folder):
    """Recursively find HTML files without following links outside root."""
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"not a folder: {root}")
    found = []
    for item in root.rglob("*"):
        try:
            if item.suffix.lower() not in (".html", ".htm") or not item.is_file():
                continue
            resolved = item.resolve(strict=True)
            relative = resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        found.append((relative.as_posix(), resolved))
    found.sort(key=lambda pair: pair[0].casefold())
    if not found:
        raise ValueError("no .html or .htm files were found in that folder")
    return root, found


def ask_html_folder():
    step_header(1, 2, "Choose the folder containing HTML files")
    while True:
        raw = safe_input(f"{C.WHITE}Folder path: {C.RESET}").strip().strip('"').strip("'")
        try:
            root, files = scan_html_files(raw)
            ok_line("HTML files found; starting the local preview")
            _SCREEN["log"].append("[saved] local HTML files scanned")
            return root, files
        except (OSError, ValueError) as exc:
            choice = ask_choice(str(exc), [("r", "enter another folder"), ("q", "quit")])
            if choice == "q":
                sys.exit(0)


def make_preview_handler(root, files, interval):
    """Serve a chrome-free carousel and safely scoped folder assets."""
    routes = ["/__iqchain/site/" + quote(rel, safe="/") for rel, _ in files]
    routes_json = json.dumps(routes, ensure_ascii=True).replace("<", "\\u003c")
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,viewport-fit=cover">
<title>IQchain V4.00 (Proxy)</title><style>
*{{box-sizing:border-box}}html,body{{width:100%;height:100%;margin:0;overflow:hidden;background:#fff}}
#frame{{position:fixed;inset:0;width:100vw;height:100vh;border:0;margin:0;padding:0;background:white;display:block}}
</style></head><body>
<iframe id="frame" title="Local preview" allow="fullscreen"></iframe>
<script>
const sites={routes_json}, delay={interval * 1000:g}; let index=0;
const frame=document.getElementById('frame');
function advance(){{if(sites.length>1)index=(index+1)%sites.length;frame.src=sites[index];}}
advance();setInterval(advance,delay);
</script></body></html>""".encode("utf-8")

    class LocalOnlyHandler(http.server.BaseHTTPRequestHandler):
        server_version = "IQchainLocal/1"
        sys_version = ""

        def _resolve(self):
            requested = unquote(urlsplit(self.path).path)
            if requested in ("/", "/index.html"):
                return "preview", None
            if requested.startswith("/__iqchain/site/"):
                requested = "/" + requested[len("/__iqchain/site/"):]
            relative = requested.lstrip("/")
            if not relative or "\x00" in relative:
                return None, None
            try:
                candidate = (root / relative).resolve(strict=True)
                candidate.relative_to(root)
                if not candidate.is_file():
                    return None, None
                return "file", candidate
            except (OSError, ValueError):
                return None, None

        def _send(self, head=False):
            kind, path = self._resolve()
            if kind is None:
                self.send_error(404)
                return
            if kind == "preview":
                body = page
                content_type = "text/html; charset=utf-8"
            else:
                try:
                    size = path.stat().st_size
                    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                    if content_type.startswith("text/") or content_type in ("application/javascript", "application/json", "image/svg+xml"):
                        content_type += "; charset=utf-8"
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(size))
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    if not head:
                        with path.open("rb") as stream:
                            shutil.copyfileobj(stream, self.wfile)
                    return
                except (OSError, BrokenPipeError):
                    return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if not head:
                try:
                    self.wfile.write(body)
                except (OSError, BrokenPipeError):
                    pass

        def do_GET(self):
            self._send(head=False)

        def do_HEAD(self):
            self._send(head=True)

        def log_message(self, fmt, *args):
            pass

    return LocalOnlyHandler


def start_local_server(root, files, start_port=8000, end_port=8300, interval=0.2):
    if not (1 <= start_port <= end_port <= 65535):
        raise ValueError("port range must satisfy 1 <= --port <= --port-end <= 65535")
    handler = make_preview_handler(root, files, interval)
    for port in range(start_port, end_port + 1):
        try:
            # Binding to loopback only: not reachable from LAN or Internet.
            server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            return server, port
        except OSError:
            continue
    raise OSError(f"no free localhost port found between {start_port} and {end_port}")


def main():
    setup_console()
    parser = argparse.ArgumentParser(description="Local-only IQchain HTML folder carousel")
    parser.add_argument("--port", type=int, default=8000, help="first localhost port to try (default: 8000)")
    parser.add_argument("--port-end", type=int, default=8300, help="last localhost fallback port (default: 8300)")
    parser.add_argument("--interval", type=float, default=0.2, help="seconds per HTML page (default: 0.2)")
    args = parser.parse_args()
    if args.interval < 0.1 or args.interval > 3600:
        parser.error("--interval must be between 0.1 and 3600 seconds")

    enter_app_screen()
    outcome = "stopped"
    error_message = None
    server = None
    try:
        print_banner()
        root, files = ask_html_folder()
        step_header(2, 2, "Start the localhost preview")
        server, port = start_local_server(root, files, args.port, args.port_end, args.interval)
        local_url = f"http://127.0.0.1:{port}/"
        ok_line(f"local preview ready at {local_url}")
        ok_line(f"automatic rotation every {args.interval:g} seconds")
        if port != args.port:
            ok_line(f"port {args.port} was busy; selected available port {port}")
        _SCREEN["log"].append("[saved] local-only server ready; no public tunnel")
        set_dashboard_header([
            f"Local: {local_url}",
            f"Automatic rotation: {args.interval:g}s",
            "Loopback only (127.0.0.1); no external link or tunnel.",
            "Open the address in a browser. Press Ctrl+C to stop.",
        ])
        start_dashboard()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        outcome = "stopped"
    except SystemExit:
        outcome = "stopped"
    except Exception as exc:
        error_message = str(exc)
    finally:
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
        leave_app_screen()
    try:
        if error_message:
            print_box([error_message], title="ERROR", color=C.ERR)
        print_farewell(outcome)
    except KeyboardInterrupt:
        pass
    return 0 if error_message is None else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(0)
