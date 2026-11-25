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
from pathlib import Path
from urllib.parse import urlparse

GITHUB_RELEASES = "https://github.com/cloudflare/cloudflared/releases/latest/download"
TRYCLOUDFLARE_RE = re.compile(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com")


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
            state["lines"].append(line.rstrip())
            m = TRYCLOUDFLARE_RE.search(line)
            if m and not state["public_url"]:
                state["public_url"] = m.group(0)
                found.set()

    threading.Thread(target=reader, daemon=True).start()

    with Spinner("opening tunnel ") as sp:
        got_link = found.wait(timeout=30)
    sp.finish("")

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
            "Press Ctrl+C here to stop the tunnel.",
        ],
        title="TUNNEL IS LIVE", color=C.OK,
    )

    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        print()
        print_box(["tunnel closed - the public link above no longer works"], title="STOPPED", color=C.GREY)
        return

    print()
    print_box(["cloudflared stopped on its own - the tunnel is no longer live"], title="TUNNEL ENDED", color=C.ERR)


# ====================================================================== #
#  MAIN                                                                   #
# ====================================================================== #
def main():
    ap = argparse.ArgumentParser(description="Expose a local URL through a Cloudflare Quick Tunnel")
    ap.add_argument("local_url", nargs="?")
    args = ap.parse_args()

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
    run_tunnel(path, local_url)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        sys.exit(0)
