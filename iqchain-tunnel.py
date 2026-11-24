#!/usr/bin/env python3
"""
cloudflared_launcher.py

Run this, give it a local URL (e.g. http://localhost:3000), and it opens
a free Cloudflare Quick Tunnel to it - no Cloudflare account needed.

What it does, in order:
  1. Checks whether `cloudflared` is already installed. If not, installs
     it automatically (Termux pkg / Homebrew / a direct binary download
     from Cloudflare's own GitHub releases, whichever fits this device).
  2. Asks for the local URL to expose.
  3. Starts `cloudflared tunnel --url <local URL>` and waits for the
     public https://...trycloudflare.com link it hands back, then shows
     it in a clean summary box.

Usage:
    python cloudflared_launcher.py
    python cloudflared_launcher.py http://localhost:3000

Standard library only on the Python side - no pip install needed.
cloudflared itself is a separate native binary, not a Python package;
this script installs that binary automatically if it's missing.
"""
import argparse
import os
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
from urllib.parse import urlparse

GITHUB_RELEASES = "https://github.com/cloudflare/cloudflared/releases/latest/download"
TRYCLOUDFLARE_RE = re.compile(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com")
PRINT_LOCK = threading.Lock()

# Alternate-screen escape codes - the same trick full-screen terminal apps
# like htop, vim and less use. Switching to the alternate buffer makes this
# program take over the whole tab/window like a real application; leaving
# it instantly restores the user's previous terminal content untouched.
_ALT_SCREEN_ON = "\033[?1049h\033[2J\033[H\033[?25l"   # switch buffer + clear + home + hide cursor
_ALT_SCREEN_OFF = "\033[?25h\033[?1049l"  # show cursor + switch buffer back

_SCREEN = {
    "active": False,          # is the full-screen dashboard currently drawn?
    "header": [],             # plain status lines pinned at the top
    "log": deque(maxlen=1000),  # cloudflared output history (plain text)
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


def _term_size():
    """(columns, rows) of the real terminal, with a safe fallback for
    environments with no real tty (works the same on Termux, Linux,
    macOS and Windows)."""
    sz = shutil.get_terminal_size(fallback=(80, 24))
    return max(sz.columns, 40), max(sz.lines, 10)


def enter_app_screen():
    """Switches into the alternate screen buffer so the program visually
    takes over the whole tab/window, like htop or vim do. No-op when
    color/tty support isn't available (CI logs, dumb terminals) so it
    never breaks a non-interactive run."""
    if _color_enabled():
        sys.stdout.write(_ALT_SCREEN_ON)
        sys.stdout.flush()


def leave_app_screen():
    """Restores the user's normal terminal exactly as it was before the
    program started - their previous scrollback is untouched."""
    if _color_enabled():
        sys.stdout.write(_ALT_SCREEN_OFF)
        sys.stdout.flush()
    _SCREEN["active"] = False


def set_dashboard_header(lines):
    """Plain-text lines (no color codes) pinned at the top of the
    full-screen dashboard, above the scrolling activity log."""
    _SCREEN["header"] = lines


def start_dashboard():
    """Switches from the normal step-by-step setup screens into the
    full-screen live dashboard: a status header + a green activity box
    that fills the rest of the terminal and scrolls like htop/top."""
    _SCREEN["active"] = True
    _redraw_dashboard()


def _redraw_dashboard():
    """Repaints the whole terminal: cursor home, clear, header block, then
    an activity block sized to use every remaining row and column, newest
    line at the bottom. Re-measures the terminal every call, so resizing
    the window is handled automatically. Drawn as one continuous frame
    with clean unicode straight lines (the header, the title and the log
    are separated by T-junctions rather than stacked separate boxes) -
    this is the one border in the program drawn this way; every other
    box (step/info/error boxes from print_box) keeps its plain style."""
    import textwrap

    cols, rows = _term_size()
    w = max(min(cols - 2, 200), 40)
    inner = w - 4
    color = C.OK

    def rule(left, right):
        return f"{color}{left}{'─' * (w - 2)}{right}{C.RESET}"

    def content_row(text):
        pad = max(inner - len(text), 0)
        return f"{color}│{C.RESET} {C.WHITE}{text}{' ' * pad} {color}│{C.RESET}"

    out = ["\033[H\033[2J"]  # cursor to top-left + clear the whole screen

    out.append(rule("┌", "┐"))
    for line in _SCREEN["header"]:
        for piece in (textwrap.wrap(line, width=inner) or [""]):
            out.append(content_row(piece))
    out.append(rule("├", "┤"))

    title = " LIVE ACTIVITY "
    pad = max(w - 2 - len(title), 0)
    left, right = pad // 2, pad - pad // 2
    out.append(f"{color}│{C.RESET}{C.BOLD}{color}{' ' * left}{title}{' ' * right}{C.RESET}{color}│{C.RESET}")
    out.append(rule("├", "┤"))

    available = max(rows - len(out) - 1, 0)  # rows left before the bottom border
    wrapped_all = []
    for line in _SCREEN["log"]:
        wrapped_all.extend(textwrap.wrap(line, width=inner) or [""])
    visible = wrapped_all[-available:] if available else []
    for _ in range(max(available - len(visible), 0)):
        out.append(f"{color}│{' ' * (w - 2)}│{C.RESET}")
    for piece in visible:
        out.append(content_row(piece))
    out.append(rule("└", "┘"))

    sys.stdout.write("\n".join(out))
    sys.stdout.flush()


def live_box_line(text):
    """Records one line of live activity (cloudflared's own output). While
    the full-screen dashboard is active it repaints the whole screen so
    the newest line appears at the bottom, like htop; before that it is
    simply kept so a failure can still show the last few lines as before -
    no change to that existing behaviour."""
    with PRINT_LOCK:
        _SCREEN["log"].append(text)
        if _SCREEN["active"]:
            _redraw_dashboard()


def live_box_close():
    """Leaves the full-screen dashboard, restoring the real terminal."""
    leave_app_screen()


# ---------------------------------------------------------------------- #
#  BRAND BANNER - IQchain logo, version and developer credit             #
# ---------------------------------------------------------------------- #
APP_NAME = "IQchain"
APP_VERSION = "v0.01"
APP_AUTHOR = "IQmaxx (DK)"

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
        choice = input(f"{C.GREY}> {C.RESET}").strip().lower()
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
            raw = input(f"{C.WHITE}Local URL (e.g. localhost:3000): {C.RESET}").strip()
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
#  TUNNEL                                                                 #
# ====================================================================== #
def run_tunnel(cloudflared_path, local_url):
    step_header(3, 3, "Open the Cloudflare tunnel")
    cmd = [cloudflared_path, "tunnel", "--url", local_url]

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
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
            return run_tunnel(cloudflared_path, local_url)
        sys.exit(0)

    print()
    print_box(
        [
            f"Local:  {local_url}",
            f"Public: {state['public_url']}",
            "",
            "This link works from anywhere while this window stays open.",
            "Switching to full-screen live view ...",
        ],
        title="TUNNEL IS LIVE", color=C.OK,
    )

    # the step-by-step history above disappears here - only the live
    # dashboard (this function's own output) stays on screen from now
    set_dashboard_header([
        f"{APP_NAME} {APP_VERSION}  (tunnel)",
        f"Local:  {local_url}",
        f"Public: {state['public_url']}",
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
    ap = argparse.ArgumentParser(description="Expose a local URL through a Cloudflare Quick Tunnel")
    ap.add_argument("local_url", nargs="?")
    args = ap.parse_args()

    # ---- the whole program, from this line to the very end, runs inside
    # the alternate screen buffer: the instant it activates it takes over
    # the full terminal/tab like a standalone application - whatever was
    # already on screen (earlier commands, scrollback) is hidden for the
    # entire run and reappears untouched only after this exits. ----------
    enter_app_screen()
    outcome = None
    try:
        print_banner()
        step_header(1, 3, "Check for cloudflared")
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

        local_url = ask_local_url(args.local_url)
        outcome = run_tunnel(path, local_url)
    except KeyboardInterrupt:      # Ctrl+C at ANY step still shows the farewell
        outcome = outcome or "stopped"
    except SystemExit:             # sys.exit() inside a step (quit choice) too
        outcome = outcome or "stopped"
    finally:
        leave_app_screen()   # restores the user's real terminal, untouched

    try:
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
