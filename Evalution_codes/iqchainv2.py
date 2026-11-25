#!/usr/bin/env python3
"""
iqchain.py  -  IQchain Framework  (v0.01)
Developed by iqmaxx (dk)

A single branded launcher that sits ON TOP of two existing, untouched
automation scripts and gives them one clean white / grey / orange
terminal UI:

    MODULE 1  ->  backen2.py   (HTML form -> JSON backend server)
    MODULE 2  ->  tunnel.py    (Cloudflare Quick Tunnel: local -> public)

IMPORTANT - by design, this file NEVER edits, imports-and-monkeypatches,
or reimplements any logic from backen2.py / tunnel.py. Both are started
as real subprocesses, exactly as they are, with their own argparse flags.
This launcher only adds: a logo, a menu, a two-tab live screen, colored
log prefixes, and the auto-wiring of Module 1's local URL into Module 2
when you run them together.

Usage:
    python iqchain.py

Keep this file in the same folder as backen2.py and tunnel.py (or point
it at them when asked). Standard library only - no pip install needed.
"""
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

APP_NAME = "IQchain"
APP_VERSION = "v0.01"
APP_AUTHOR = "iqmaxx (dk)"

LOCAL_URL_RE = re.compile(r"Open in browser\s*:\s*(\S+)")
PUBLIC_URL_RE = re.compile(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com")


# ====================================================================== #
#  THEME  -  white / grey text, single orange accent (same palette the  #
#  two modules already use, so everything feels like one product)       #
# ====================================================================== #
def _color_enabled():
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR") is not None:
        return True
    return sys.stdout.isatty()


class Theme:
    ORANGE = "\033[38;5;214m"
    ORANGE_BOLD = "\033[1m\033[38;5;214m"
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

# Tags used to tell the two modules' log lines apart on one shared screen.
TAG1 = "MOD-1 BACKEND"
TAG2 = "MOD-2 TUNNEL "


def _term_width():
    try:
        return max(50, min(os.get_terminal_size().columns - 2, 78))
    except Exception:
        return 70


# ====================================================================== #
#  LOGO  -  blocky "IQCHAIN" wordmark, built from a tiny 5x5 font map    #
# ====================================================================== #
_FONT = {
    "I": ["█████", "  █  ", "  █  ", "  █  ", "█████"],
    "Q": [" ███ ", "█   █", "█   █", "█  ██", " ████"],
    "C": [" ████", "█    ", "█    ", "█    ", " ████"],
    "H": ["█   █", "█   █", "█████", "█   █", "█   █"],
    "A": [" ███ ", "█   █", "█████", "█   █", "█   █"],
    "N": ["█   █", "██  █", "█ █ █", "█  ██", "█   █"],
}


def _wordmark(word):
    rows = ["", "", "", "", ""]
    for ch in word:
        glyph = _FONT.get(ch.upper(), ["     "] * 5)
        for i in range(5):
            rows[i] += glyph[i] + " "
    return rows


def print_logo():
    print()
    for line in _wordmark("IQCHAIN"):
        sys.stdout.write(f"{C.ORANGE_BOLD}{line}{C.RESET}\n")
    tagline = f"{APP_NAME}  {APP_VERSION}   -   developed by {APP_AUTHOR}"
    w = _term_width()
    pad = max((w - len(tagline)) // 2, 0)
    sys.stdout.write(f"{C.GREY}{' ' * pad}{tagline}{C.RESET}\n")
    sys.stdout.write(f"{C.GREY}{'-' * w}{C.RESET}\n\n")


# ====================================================================== #
#  BOX / UI HELPERS  (same drawing style the two modules already use)   #
# ====================================================================== #
def print_box(lines, title=None, color=None):
    import textwrap

    color = color or C.GREY
    w = _term_width()
    inner = w - 4
    border = f"{color}+{'-' * (w - 2)}+{C.RESET}"
    out = [border]
    if title:
        t = f" {title} "
        pad = max(w - 2 - len(t), 0)
        left, right = pad // 2, pad - pad // 2
        out.append(f"{color}|{C.RESET}{C.BOLD}{C.ORANGE}{' ' * left}{t}{' ' * right}{C.RESET}{color}|{C.RESET}")
        out.append(border)
    for line in lines:
        if line == "":
            out.append(f"{color}|{' ' * (w - 2)}|{C.RESET}")
            continue
        for piece in (textwrap.wrap(line, width=inner) or [""]):
            pad = max(inner - len(piece), 0)
            out.append(f"{color}| {C.RESET}{C.WHITE}{piece}{' ' * pad} {color}|{C.RESET}")
    out.append(border)
    sys.stdout.write("\n".join(out) + "\n")
    sys.stdout.flush()


def print_tabs(active):
    """A small tab bar across the top of the screen. `active` is
    1, 2, "both" or None - purely visual, shows which module(s) are
    live and offers a [+] to show a second one can be attached."""
    def tab(label, on):
        c = C.ORANGE_BOLD if on else C.GREY
        return f"{c}[ {label} ]{C.RESET}"

    on1 = active in (1, "both")
    on2 = active in (2, "both")
    plus = f"{C.DIM}{C.GREY}[ + ]{C.RESET}" if not (on1 and on2) else f"{C.OK}[ ✓ ]{C.RESET}"
    sys.stdout.write(f"  {tab('TAB 1 : BACKEND', on1)}   {plus}   {tab('TAB 2 : TUNNEL', on2)}\n\n")


def ask_choice(message, options, title="WHAT WOULD YOU LIKE TO DO?"):
    opt_line = "   ".join(f"[{k.upper()}] {label}" for k, label in options)
    print_box([message, "", opt_line], title=title, color=C.ORANGE)
    valid = {k.lower() for k, _ in options}
    while True:
        choice = input(f"{C.GREY}> {C.RESET}").strip().lower()
        if choice in valid:
            return choice
        sys.stdout.write(f"{C.ERR}  please pick one of: {', '.join(k.upper() for k, _ in options)}{C.RESET}\n")


def ask_text(prompt, default=None):
    suffix = f" [{default}]" if default else ""
    raw = input(f"{C.WHITE}{prompt}{suffix}: {C.RESET}").strip()
    return raw or default


def ok_line(text):
    sys.stdout.write(f"{C.OK}  OK  {C.RESET}{C.WHITE}{text}{C.RESET}\n")


# ====================================================================== #
#  LOCATING THE TWO UNTOUCHED MODULES                                    #
# ====================================================================== #
def locate_module(filename, nice_name):
    here = Path(__file__).resolve().parent
    candidate = here / filename
    if candidate.is_file():
        return candidate
    print_box(
        [f"{filename} was not found next to iqchain.py in {here}",
         f"Enter the full path to {nice_name} (or leave blank to quit):"],
        title="MODULE NOT FOUND", color=C.ERR,
    )
    while True:
        raw = input(f"{C.WHITE}{filename} path: {C.RESET}").strip().strip('"').strip("'")
        if not raw:
            sys.exit(0)
        p = Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
        if p.is_file():
            return p
        sys.stdout.write(f"{C.ERR}  not a file: {p}{C.RESET}\n")


# ====================================================================== #
#  SUBPROCESS RUNNER  -  streams a module's own output, tagged + colored #
# ====================================================================== #
class ModuleRun:
    """Wraps one subprocess, tees its stdout to the shared screen with a
    colored tag, and lets the caller watch for a line matching a regex
    (used to grab Module 1's local URL and Module 2's public URL)."""

    def __init__(self, cmd, tag, color):
        self.cmd = cmd
        self.tag = tag
        self.color = color
        self.proc = None
        self.lines = []
        self._watchers = []
        self._lock = threading.Lock()

    def watch(self, pattern):
        """Returns a threading.Event that gets set the first time a
        line matching `pattern` is seen; event.result holds the match."""
        ev = threading.Event()
        ev.result = None
        with self._lock:
            self._watchers.append((pattern, ev))
        return ev

    def start(self):
        self.proc = subprocess.Popen(
            self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        threading.Thread(target=self._pump, daemon=True).start()
        return self.proc

    def _pump(self):
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            self.lines.append(line)
            sys.stdout.write(f"{self.color}[{self.tag}]{C.RESET} {C.WHITE}{line}{C.RESET}\n")
            sys.stdout.flush()
            with self._lock:
                for pattern, ev in self._watchers:
                    if not ev.is_set():
                        m = pattern.search(line)
                        if m:
                            ev.result = m
                            ev.set()

    def terminate(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def wait(self):
        if self.proc:
            self.proc.wait()


# ====================================================================== #
#  MODULE 1  -  BACKEND  (backen2.py, untouched)                         #
# ====================================================================== #
def collect_backend_args(backend_path):
    print_tabs(1)
    print_box(["Module 1 turns any HTML file into a tiny local backend",
                "that saves every form submit to a JSON file."],
               title="MODULE 1 : BACKEND SETUP", color=C.ORANGE)
    html = ask_text("HTML file to serve")
    while not html:
        html = ask_text("HTML file to serve")
    port = ask_text("Port", default="8000")
    host = ask_text("Host (127.0.0.1 = this device only, 0.0.0.0 = LAN)", default="127.0.0.1")
    hash_pw = ask_choice("Store passwords as typed, or hashed (PBKDF2)?",
                          [("p", "plain (as typed)"), ("h", "hashed")],
                          title="PASSWORD STORAGE")
    cmd = [sys.executable, str(backend_path), html, "--port", str(port), "--host", host]
    if hash_pw == "h":
        cmd.append("--hash-passwords")
    return cmd


def run_backend_standalone(backend_path):
    cmd = collect_backend_args(backend_path)
    sys.stdout.write(f"\n{C.GREY}launching module 1 ...{C.RESET}\n\n")
    subprocess.run(cmd)


# ====================================================================== #
#  MODULE 2  -  TUNNEL  (tunnel.py, untouched)                           #
# ====================================================================== #
def run_tunnel_standalone(tunnel_path):
    print_tabs(2)
    print_box(["Module 2 opens a free Cloudflare Quick Tunnel so any",
                "local URL gets a public https://...trycloudflare.com link."],
               title="MODULE 2 : TUNNEL SETUP", color=C.ORANGE)
    local_url = ask_text("Local URL to expose (leave blank to be asked by the tunnel itself)")
    cmd = [sys.executable, str(tunnel_path)] + ([local_url] if local_url else [])
    sys.stdout.write(f"\n{C.GREY}launching module 2 ...{C.RESET}\n\n")
    subprocess.run(cmd)


# ====================================================================== #
#  COMBINED  -  MODULE 1 + MODULE 2 ON ONE SCREEN                        #
# ====================================================================== #
def run_combined(backend_path, tunnel_path):
    print_tabs("both")
    print_box(
        ["Module 1 (backend) starts first and serves your HTML form.",
         "Module 2 (tunnel) then auto-attaches to that local address",
         "and turns it into a public link. Both logs stream below,",
         "tagged, on this one screen."],
        title="RUN BOTH MODULES TOGETHER", color=C.ORANGE,
    )

    backend_cmd = collect_backend_args(backend_path)
    mod1 = ModuleRun(backend_cmd, TAG1, C.WHITE)
    local_ready = mod1.watch(LOCAL_URL_RE)

    print()
    print_tabs("both")
    sys.stdout.write(f"{C.GREY}starting module 1 (backend)...{C.RESET}\n\n")
    mod1.start()

    if not local_ready.wait(timeout=20):
        print_box(["module 1 did not report a local URL within 20 seconds",
                    "check the backend log above for the real error"],
                   title="TIMED OUT", color=C.ERR)
        mod1.terminate()
        return
    local_url = local_ready.result.group(1).rstrip("/")
    ok_line(f"module 1 is live at: {local_url}")

    tunnel_cmd = [sys.executable, str(tunnel_path), local_url]
    mod2 = ModuleRun(tunnel_cmd, TAG2, C.ORANGE)
    public_ready = mod2.watch(PUBLIC_URL_RE)

    sys.stdout.write(f"\n{C.GREY}starting module 2 (tunnel) -> {local_url} ...{C.RESET}\n\n")
    mod2.start()

    dashboard_shown = False
    if public_ready.wait(timeout=30):
        public_url = public_ready.result.group(0)
        print()
        print_box(
            [f"Local  (Module 1):  {local_url}",
             f"Public (Module 2):  {public_url}",
             "",
             "Both modules are running on this one screen.",
             "Press Ctrl+C to stop them both."],
            title=f"{APP_NAME} IS LIVE", color=C.OK,
        )
        dashboard_shown = True
    else:
        print_box(["module 2 has not reported a public link yet - still",
                    "streaming its log below; it may still appear"],
                   title="HEADS UP", color=C.GREY)

    try:
        while mod1.proc.poll() is None and mod2.proc.poll() is None:
            time.sleep(0.3)
    except KeyboardInterrupt:
        pass
    finally:
        mod1.terminate()
        mod2.terminate()
        mod1.wait()
        mod2.wait()
        print()
        print_box(["both modules stopped - the public link above no longer works"]
                   if dashboard_shown else ["both modules stopped"],
                   title=f"{APP_NAME} STOPPED", color=C.GREY)


# ====================================================================== #
#  MAIN MENU                                                              #
# ====================================================================== #
def main_menu():
    backend_path = locate_module("backen2.py", "Module 1 (backen2.py)")
    tunnel_path = locate_module("tunnel.py", "Module 2 (tunnel.py)")

    while True:
        print_logo()
        print_tabs(None)
        choice = ask_choice(
            f"{APP_NAME} wraps both modules in one framework. Pick what to run:",
            [
                ("1", "Module 1 only (Backend)"),
                ("2", "Module 2 only (Tunnel)"),
                ("3", "Both together  (recommended)"),
                ("q", "Quit"),
            ],
            title=f"{APP_NAME} {APP_VERSION} - MAIN MENU",
        )
        print()
        if choice == "1":
            run_backend_standalone(backend_path)
        elif choice == "2":
            run_tunnel_standalone(tunnel_path)
        elif choice == "3":
            run_combined(backend_path, tunnel_path)
        elif choice == "q":
            sys.stdout.write(f"\n{C.GREY}{APP_NAME} {APP_VERSION} - goodbye.{C.RESET}\n")
            return
        print()


if __name__ == "__main__":
    try:
        main_menu()
    except KeyboardInterrupt:
        print()
        sys.exit(0)
