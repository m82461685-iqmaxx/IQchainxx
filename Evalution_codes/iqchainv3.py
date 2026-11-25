#!/usr/bin/env python3
"""
IQchain  -  v0.02  developed by iqmaxx (dk)

A real full-screen terminal FRAMEWORK, not a tab/tmux launcher.

It takes over the whole terminal (the alternate screen buffer) and draws
exactly ONE frame, white/grey text on an orange-accented border, with the
IQchain logo on top. Press "+" once (no Enter needed) and that SAME frame
splits itself in place into two live panes, side by side, on that SAME
single screen:

    left  pane  = Module 1 (backen2.py)  -  backend, local host shown live
    right pane  = Module 2 (tunnel.py)   -  local host turned into a
                                             public link, shown live

Nothing in backen2.py or tunnel.py is edited, re-typed, or re-implemented.
Both are imported as-is and this frame only:
  1. captures their printed output line-by-line into its two panes, and
  2. pins the two facts the sketch asked for ("Local Host: ..." on the
     left, "Public ....com" on the right) the moment each module prints
     them - by reading their output, never by touching their code.

Keys, once the frame is up:
    [ + ]   split the single frame and run both modules live
    [ 1 ]   run Module 1 only (still full-screen, single pane)
    [ 2 ]   run Module 2 only (still full-screen, single pane)
    [ q ]   quit
    Ctrl+C  stop both modules / leave the frame cleanly, any time

Usage:
    python iqchain.py
    python iqchain.py page.html --port 8000
"""
import argparse
import os
import re
import select
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND_SCRIPT = HERE / "backen2.py"
TUNNEL_SCRIPT = HERE / "tunnel.py"
IS_WINDOWS = os.name == "nt"

if not IS_WINDOWS:
    import termios
    import tty
else:
    import msvcrt  # noqa: F401  (Windows console input)

# backen2.py and tunnel.py are imported untouched - zero lines of either
# file are copied or re-typed here. Their own color theme is reused as-is
# so this frame's orange/white/grey never drifts from theirs.
sys.path.insert(0, str(HERE))
from tunnel import Theme, _color_enabled, TRYCLOUDFLARE_RE  # noqa: E402
from backen2 import clean_path as _clean_path  # noqa: E402

C = Theme() if _color_enabled() else type("_NoTheme", (), {"__getattr__": lambda s, n: ""})()

VERSION_TAG = "v0.02  developed by iqmaxx (dk)"

LOGO = [
    r" ___ ___  _____ _  _   _   ___ _  _ ",
    r"|_ _/ _ \|_   _| || | /_\ |_ _| \| |",
    r" | | (_) | | | | __ |/ _ \ | || .` |",
    r"|___\__\_\ |_| |_||_/_/ \_\___|_|\_|",
]

LOCAL_HOST_RE = re.compile(r"https?://[0-9A-Za-z.\-]+:\d+")


# ====================================================================== #
#  LOW-LEVEL SCREEN - ANSI alternate-screen, cursor addressing, keys     #
# ====================================================================== #
ESC = "\033"


def _goto(row, col):
    return f"{ESC}[{row};{col}H"


class Screen:
    """One owner of the whole terminal for the life of the program. Every
    redraw writes one complete string from the top-left corner, every row
    padded to full width, so the previous frame is overwritten in place -
    this is what keeps it feeling like a single still frame instead of
    scrolling text."""

    def __init__(self):
        self._old_attrs = None

    def open(self):
        sys.stdout.write(f"{ESC}[?1049h{ESC}[?25l{ESC}[2J")
        sys.stdout.flush()
        if not IS_WINDOWS:
            self._old_attrs = termios.tcgetattr(sys.stdin.fileno())

    def close(self):
        if not IS_WINDOWS and self._old_attrs is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_attrs)
        sys.stdout.write(f"{ESC}[?25h{ESC}[?1049l")
        sys.stdout.flush()

    @contextmanager
    def cooked(self):
        """Normal line-editing mode, for the one or two plain input()
        prompts (HTML path / port) - still inside the same alt-screen."""
        if IS_WINDOWS or self._old_attrs is None:
            yield
            return
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._old_attrs)
        try:
            yield
        finally:
            tty.setcbreak(sys.stdin.fileno())

    def cbreak(self):
        if not IS_WINDOWS:
            tty.setcbreak(sys.stdin.fileno())

    def size(self):
        c, r = shutil.get_terminal_size(fallback=(100, 32))
        return max(c, 60), max(r, 20)

    def draw(self, lines):
        cols, rows = self.size()
        out = [f"{ESC}[H"]
        for i in range(rows):
            line = lines[i] if i < len(lines) else ""
            visible = _strip_ansi(line)
            fill = max(cols - len(visible), 0)
            out.append(line + " " * fill)
            if i != rows - 1:
                out.append("\n")
        sys.stdout.write("".join(out))
        sys.stdout.flush()


_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _strip_ansi(s):
    return _ANSI_RE.sub("", s)


def poll_key(timeout=0.1):
    """Non-blocking: returns one keypress (no Enter needed) or None."""
    if IS_WINDOWS:
        t0 = time.time()
        while time.time() - t0 < timeout:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                return ch
            time.sleep(0.02)
        return None
    r, _, _ = select.select([sys.stdin], [], [], timeout)
    if r:
        return sys.stdin.read(1)
    return None


# ====================================================================== #
#  FRAME DRAWING                                                         #
# ====================================================================== #
def _center(text, width):
    text = text[:width]
    pad = max(width - len(text), 0)
    left = pad // 2
    return " " * left + text + " " * (pad - left)


def _box_top(width, ch="-"):
    return "+" + ch * (width - 2) + "+"


def _row(text, width, color=None):
    color = color or ""
    inner = width - 4
    text = text[:inner]
    fill = inner - len(text)
    return f"{C.GREY}|{C.RESET} {color}{text}{C.RESET}{' ' * fill} {C.GREY}|{C.RESET}"


def _divider(width):
    return f"{C.GREY}+" + "-" * (width - 2) + f"+{C.RESET}"


def render_single_frame(cols, rows, html_val, port_val, host_val, menu_hint, status="(hang on)"):
    """The one un-split frame - matches the sketch: logo on top, a dashed
    divider splitting the body visually into Module 1 / Module 2 columns
    even before the real split happens."""
    w = cols - 2
    half = w // 2
    lines = []
    lines.append(f"{C.GREY}{_box_top(w)}{C.RESET}")
    lines.append(_row(_center(f"({status})", w - 4), w))
    lines.append(_row("", w))
    for logo_line in LOGO:
        lines.append(_row(f"{C.BOLD}{C.ORANGE}{_center(logo_line, w - 4)}{C.RESET}", w))
    lines.append(_row(_center(VERSION_TAG, w - 4, ), w, color=C.GREY))
    lines.append(_divider(w))

    def split_row(left, right, color=None):
        l_inner = half - 3
        r_inner = (w - half) - 3
        left = (left or "")[:l_inner].ljust(l_inner)
        right = (right or "")[:r_inner].ljust(r_inner)
        c = color or ""
        return (f"{C.GREY}|{C.RESET} {c}{left}{C.RESET}{C.GREY}:{C.RESET} "
                f"{c}{right}{C.RESET}{' ' * max(w - half - r_inner - 4, 0)}{C.GREY}|{C.RESET}")

    lines.append(split_row(f"{C.BOLD}MODULE - 1 (backend){C.RESET}",
                            f"{C.BOLD}MODULE - 2 (tunnel){C.RESET}"))
    lines.append(_divider(w))
    lines.append(split_row(f"* HTML file path: {html_val or '(not set yet)'}",
                            f"feed the local host: {host_val}"))
    lines.append(split_row("", ""))
    lines.append(split_row(f"* Local Host: 127.0.0.1:{port_val}",
                            "waiting for + to start module 2 ..."))
    for _ in range(max(rows - len(lines) - 5, 0)):
        lines.append(split_row("", ""))
    lines.append(_divider(w))
    lines.append(_row(menu_hint, w, color=C.ORANGE))
    lines.append(f"{C.GREY}{_box_top(w)}{C.RESET}")
    return lines


def render_split_frame(cols, rows, m1_lines, m2_lines, m1_pin, m2_pin, footer):
    """Both modules live, side by side, in the same screen."""
    w = cols
    half = w // 2
    l_w, r_w = half, w - half
    lines = []
    header_l = _center(f"{C.BOLD}MODULE 1 - BACKEND{C.RESET}", l_w - 2)
    header_r = _center(f"{C.BOLD}MODULE 2 - TUNNEL{C.RESET}", r_w - 2)
    lines.append(f"{C.GREY}{'-' * (l_w - 1)}+{'-' * (r_w - 1)}{C.RESET}")
    lines.append(f"{C.GREY}|{C.RESET}{header_l}{C.GREY}|{C.RESET}{header_r}")
    lines.append(f"{C.GREY}{'-' * (l_w - 1)}+{'-' * (r_w - 1)}{C.RESET}")

    pin_h = 0
    if m1_pin or m2_pin:
        left_pin = [f" {C.OK}Local Host:{C.RESET} {m1_pin}"] if m1_pin else [""]
        right_pin = ["", f" {C.BOLD}{C.ORANGE}TUNNEL ON LIVE{C.RESET}",
                      f" Local:  {m1_pin or '...'}",
                      f" Public: {C.OK}{m2_pin}{C.RESET}" if m2_pin else " Public: (waiting ...)"]
        pin_h = max(len(left_pin), len(right_pin))
        for i in range(pin_h):
            l = left_pin[i] if i < len(left_pin) else ""
            r = right_pin[i] if i < len(right_pin) else ""
            lines.append(_side_by_side(l, r, l_w, r_w))
        lines.append(f"{C.GREY}{'-' * (l_w - 1)}+{'-' * (r_w - 1)}{C.RESET}")

    body_rows = rows - len(lines) - 2
    body_rows = max(body_rows, 1)
    m1_tail = list(m1_lines)[-body_rows:]
    m2_tail = list(m2_lines)[-body_rows:]
    for i in range(body_rows):
        l = m1_tail[i] if i < len(m1_tail) else ""
        r = m2_tail[i] if i < len(m2_tail) else ""
        lines.append(_side_by_side(l, r, l_w, r_w))

    lines.append(f"{C.GREY}{'-' * (l_w - 1)}+{'-' * (r_w - 1)}{C.RESET}")
    lines.append(_row_full(footer, w, color=C.ORANGE))
    return lines


def _side_by_side(left, right, l_w, r_w):
    left = _strip_ansi(left) if False else left  # keep color, just truncate by visible length
    l_text, l_len = _truncate_visible(left, l_w - 2)
    r_text, r_len = _truncate_visible(right, r_w - 2)
    return (f" {l_text}{C.RESET}{' ' * (l_w - 2 - l_len)}{C.GREY}|{C.RESET} "
            f"{r_text}{C.RESET}{' ' * (r_w - 2 - r_len)}")


def _truncate_visible(s, width):
    """Truncate to `width` VISIBLE characters while keeping ANSI codes."""
    out = []
    visible = 0
    i = 0
    while i < len(s) and visible < width:
        m = _ANSI_RE.match(s, i)
        if m:
            out.append(m.group(0))
            i = m.end()
            continue
        out.append(s[i])
        visible += 1
        i += 1
    return "".join(out), visible


def _row_full(text, width, color=None):
    color = color or ""
    text = text[:width - 2]
    return f" {color}{text}{C.RESET}" + " " * (width - 2 - len(text))


# ====================================================================== #
#  INPUT (still inside the one alt-screen frame)                         #
# ====================================================================== #
def ask_html_path(screen, cli_value, port_placeholder):
    raw = cli_value
    while True:
        cols, rows = screen.size()
        screen.draw(render_single_frame(
            cols, rows, raw or "", port_placeholder, "127.0.0.1",
            "Type the path to your HTML file and press Enter"))
        if not raw:
            with screen.cooked():
                sys.stdout.write(_goto(rows - 3, 5))
                sys.stdout.flush()
                raw = input(f"{C.WHITE}HTML file path: {C.RESET}").strip()
        path = _clean_path(raw) if raw else None
        if path and path.resolve().is_file():
            return path.resolve()
        raw = None  # ask again


def ask_port(screen, cli_value, html_val):
    if cli_value:
        return cli_value
    cols, rows = screen.size()
    screen.draw(render_single_frame(
        cols, rows, str(html_val), "8000", "127.0.0.1",
        "Port for Module 1 - press Enter for 8000"))
    with screen.cooked():
        sys.stdout.write(_goto(rows - 3, 5))
        sys.stdout.flush()
        raw = input(f"{C.WHITE}Local port for Module 1 (Enter for 8000): {C.RESET}").strip()
    if not raw:
        return 8000
    try:
        return int(raw)
    except ValueError:
        return 8000


# ====================================================================== #
#  RUNNING THE TWO MODULES - captured, never touched                    #
# ====================================================================== #
class ModuleRunner:
    """Runs one module (backen2.py or tunnel.py) as its own real process -
    their code, verbatim, in its own interpreter - and captures stdout
    line by line into a bounded scroll buffer this frame can render."""

    def __init__(self, cmd, maxlen=400):
        self.cmd = cmd
        self.lines = deque(maxlen=maxlen)
        self.pin = None
        self.pin_re = None
        self.proc = None
        self._thread = None

    def start(self):
        self.proc = subprocess.Popen(
            self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self):
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            self.lines.append(line)
            if self.pin_re:
                m = self.pin_re.search(line)
                if m:
                    self.pin = m.group(0)
        self.lines.append(f"{C.GREY}(module ended){C.RESET}")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


def _module_cmds(html_path, port):
    local_url = f"http://127.0.0.1:{port}"
    cmd1 = [sys.executable, str(BACKEND_SCRIPT), str(html_path), "--port", str(port)]
    cmd2 = [sys.executable, str(TUNNEL_SCRIPT), local_url]
    return cmd1, cmd2


def run_split(screen, html_path, port):
    cmd1, cmd2 = _module_cmds(html_path, port)
    m1 = ModuleRunner(cmd1)
    m1.pin_re = LOCAL_HOST_RE
    m2 = ModuleRunner(cmd2)
    m2.pin_re = TRYCLOUDFLARE_RE
    m1.start()
    m2.start()
    try:
        while True:
            cols, rows = screen.size()
            screen.draw(render_split_frame(
                cols, rows, m1.lines, m2.lines, m1.pin, m2.pin,
                "Ctrl+C stops both modules"))
            key = poll_key(0.15)
            if key == "q":
                break
            if m1.proc.poll() is not None and m2.proc.poll() is not None:
                time.sleep(1.0)
                break
    except KeyboardInterrupt:
        pass
    finally:
        m1.stop()
        m2.stop()


def run_single(screen, cmd, title, pin_re=None):
    m = ModuleRunner(cmd)
    m.pin_re = pin_re
    m.start()
    try:
        while True:
            cols, rows = screen.size()
            lines = [f"{C.GREY}{_box_top(cols)}{C.RESET}",
                      _row(_center(title, cols - 4), cols, color=C.BOLD + C.ORANGE)]
            if m.pin:
                lines.append(_row(f"-> {m.pin}", cols, color=C.OK))
            lines.append(_divider(cols))
            body_rows = rows - len(lines) - 2
            for line in list(m.lines)[-body_rows:]:
                lines.append(_row(line, cols))
            while len(lines) < rows - 1:
                lines.append(_row("", cols))
            lines.append(_row("Ctrl+C stops this module", cols, color=C.ORANGE))
            screen.draw(lines)
            key = poll_key(0.15)
            if key == "q":
                break
            if m.proc.poll() is not None:
                time.sleep(1.0)
                break
    except KeyboardInterrupt:
        pass
    finally:
        m.stop()


# ====================================================================== #
#  MAIN                                                                   #
# ====================================================================== #
def main():
    ap = argparse.ArgumentParser(description="IQchain - one frame, two live modules")
    ap.add_argument("html", nargs="?", help="path to the HTML file (Module 1)")
    ap.add_argument("--port", type=int, help="local port for Module 1 (default 8000)")
    args = ap.parse_args()

    screen = Screen()
    screen.open()
    screen.cbreak()
    try:
        html_path = ask_html_path(screen, args.html, args.port or 8000)
        screen.cbreak()
        port = ask_port(screen, args.port, html_path)
        screen.cbreak()

        menu = ("[ + ] split the frame - run both      "
                "[ 1 ] module 1 only      [ 2 ] module 2 only      [ q ] quit")
        while True:
            cols, rows = screen.size()
            screen.draw(render_single_frame(cols, rows, str(html_path), port,
                                             "127.0.0.1", menu, status="ready"))
            key = poll_key(0.15)
            if key is None:
                continue
            key = key.lower()
            if key == "q":
                break
            if key == "+":
                run_split(screen, html_path, port)
                break
            if key == "1":
                cmd1, _ = _module_cmds(html_path, port)
                run_single(screen, cmd1, "MODULE 1 - BACKEND", LOCAL_HOST_RE)
                break
            if key == "2":
                _, cmd2 = _module_cmds(html_path, port)
                run_single(screen, cmd2, "MODULE 2 - TUNNEL", TRYCLOUDFLARE_RE)
                break
    finally:
        screen.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
