#!/usr/bin/env python3
"""
IQchain  -  v0.03  developed by iqmaxx (dk)

One full-screen terminal frame. Everything is typed INSIDE the frame:

    python IQchain.py

  1. Module 1 (backend) fills the whole screen:
        HTML file path  -> type / paste it, press Enter
        Local Host port -> type it,           press Enter  => backend starts
  2. Press  Tab  (or click the  [ + ]  button, or press "+")  and the SAME
     frame splits: Module 2 (tunnel) opens on the right.
        feed the local host -> type the port, press Enter  => tunnel starts
        "TUNNEL ON LIVE" box shows Local Host + Public link.

Mouse : click a box to put the cursor there (click [ + ] to split)
Keys  : Tab / Shift+Tab / Up / Down  move between boxes
        Ctrl+Y  copy the public link      Ctrl+C  stop everything and quit

backen2.py and tunnel.py are NOT modified - they are run as they are.
"""
import argparse
import base64
import codecs
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import unquote

HERE = Path(__file__).resolve().parent
BACKEND_SCRIPT = HERE / "backen2.py"
TUNNEL_SCRIPT = HERE / "tunnel.py"
IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    import msvcrt
else:
    import termios
    import tty

VERSION_TAG = "v0.03  developed by iqmaxx (dk)"

# public-link detector: reuse tunnel.py's own pattern when available
PUBLIC_RE = re.compile(r"https://[A-Za-z0-9\-]+\.trycloudflare\.com")
try:
    sys.path.insert(0, str(HERE))
    from tunnel import TRYCLOUDFLARE_RE as PUBLIC_RE  # noqa: E402,F811
except Exception:
    pass

# ---------------------------------------------------------------- styles
ESC = "\033"
RESET = ESC + "[0m"
BOLD = ESC + "[1m"
REV = ESC + "[7m"
ORANGE = ESC + "[38;5;208m"
WHITE = ESC + "[97m"
GREY = ESC + "[38;5;245m"
GREEN = ESC + "[92m"
RED = ESC + "[91m"

# ------------------------------------------------------------------ logo
_FONT = {
    "I": ["█████", "  █  ", "  █  ", "  █  ", "█████"],
    "Q": [" ███ ", "█   █", "█   █", "█  █ ", " ██ █"],
    "c": ["     ", " ████", "█    ", "█    ", " ████"],
    "h": ["█    ", "█    ", "████ ", "█   █", "█   █"],
    "a": ["     ", " ███ ", "    █", " ████", " ████"],
    "i": ["█", " ", "█", "█", "█"],
    "n": ["     ", "████ ", "█   █", "█   █", "█   █"],
}
LOGO = ["   ".join(_FONT[ch][r] for ch in "IQchain") for r in range(5)]


# ---------------------------------------------------------------- canvas
class Canvas:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.cells = [[(" ", "")] * w for _ in range(h)]

    def put(self, x, y, text, style="", maxx=None):
        if not 0 <= y < self.h:
            return
        lim = self.w - 1 if maxx is None else min(maxx, self.w - 1)
        for i, ch in enumerate(text):
            if x + i > lim:
                break
            if x + i >= 0:
                self.cells[y][x + i] = (ch, style)

    def center(self, y, text, style=""):
        self.put((self.w - len(text)) // 2, y, text, style)

    def box(self, x0, y0, x1, y1, style, title=""):
        self.put(x0, y0, "┌" + "─" * (x1 - x0 - 1) + "┐", style)
        self.put(x0, y1, "└" + "─" * (x1 - x0 - 1) + "┘", style)
        for y in range(y0 + 1, y1):
            self.put(x0, y, "│", style)
            self.put(x1, y, "│", style)
        if title:
            self.put(x0 + 2, y0, title, style)

    def render(self):
        rows = []
        for row in self.cells:
            out, prev = [], None
            for ch, st in row:
                if st != prev:
                    out.append(RESET + st)
                    prev = st
                out.append(ch)
            out.append(RESET)
            rows.append("".join(out))
        return "\n".join(rows)


# ----------------------------------------------------------------- field
class Field:
    def __init__(self, text=""):
        self.text, self.cur = text, len(text)

    def insert(self, t):
        self.text = self.text[:self.cur] + t + self.text[self.cur:]
        self.cur += len(t)

    def backspace(self):
        if self.cur:
            self.text = self.text[:self.cur - 1] + self.text[self.cur:]
            self.cur -= 1

    def delete(self):
        self.text = self.text[:self.cur] + self.text[self.cur + 1:]

    def left(self):
        self.cur = max(0, self.cur - 1)

    def right(self):
        self.cur = min(len(self.text), self.cur + 1)

    def home(self):
        self.cur = 0

    def end(self):
        self.cur = len(self.text)

    def clear(self):
        self.text, self.cur = "", 0


# ---------------------------------------------------------------- module
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean_line(s):
    s = s.rstrip("\r\n")
    if "\r" in s:
        parts = [p for p in s.split("\r") if p.strip()]
        s = parts[-1] if parts else ""
    s = _ANSI.sub("", s).replace("\t", "    ")
    return _CTRL.sub("", s)


class Module:
    """Runs one script as its own process, captures its output."""

    def __init__(self, pin_re=None):
        self.lines = deque(maxlen=300)
        self.pin = None
        self.pin_re = pin_re
        self.proc = None
        self.target = ""

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, cmd):
        self.stop()
        self.lines.clear()
        self.pin = None
        kw = {}
        if not IS_WINDOWS:
            kw["start_new_session"] = True
        try:
            self.proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
                errors="replace", env={**os.environ, "PYTHONUNBUFFERED": "1"}, **kw)
        except OSError as e:
            self.proc = None
            self.lines.append(f"cannot start: {e}")
            return
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc):
        for raw in proc.stdout:
            line = clean_line(raw)
            if not line.strip():
                continue
            self.lines.append(line)
            if self.pin_re and proc is self.proc:
                m = self.pin_re.search(line)
                if m:
                    self.pin = m.group(0)
        if proc is self.proc:
            self.lines.append("(module ended)")

    def stop(self):
        p = self.proc
        if not p or p.poll() is not None:
            return
        try:
            if IS_WINDOWS:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                               capture_output=True)
            else:
                os.killpg(p.pid, signal.SIGTERM)
                try:
                    p.wait(2)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass


# --------------------------------------------------------------- terminal
class Term:
    def __init__(self):
        self.old = None
        self.dec = codecs.getincrementaldecoder("utf-8")(errors="ignore")

    def open(self):
        if IS_WINDOWS:
            os.system("")
        else:
            self.old = termios.tcgetattr(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())
        sys.stdout.write(f"{ESC}[?1049h{ESC}[?25l{ESC}[?1000h{ESC}[?1006h{ESC}[2J")
        sys.stdout.flush()

    def close(self):
        sys.stdout.write(f"{ESC}[?1006l{ESC}[?1000l{ESC}[?25h{ESC}[?1049l")
        sys.stdout.flush()
        if self.old is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self.old)

    def size(self):
        c, r = shutil.get_terminal_size(fallback=(100, 30))
        return max(c, 60), max(r, 16)

    def read(self, timeout):
        if IS_WINDOWS:
            end, buf = time.time() + timeout, ""
            keys = {"H": "\x1b[A", "P": "\x1b[B", "K": "\x1b[D", "M": "\x1b[C",
                    "G": "\x1b[H", "O": "\x1b[F", "S": "\x1b[3~"}
            while True:
                while msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("\x00", "\xe0"):
                        buf += keys.get(msvcrt.getwch(), "")
                    else:
                        buf += ch
                if buf or time.time() >= end:
                    return buf
                time.sleep(0.02)
        fd = sys.stdin.fileno()
        r, _, _ = select.select([fd], [], [], timeout)
        if r:
            return self.dec.decode(os.read(fd, 4096))
        return ""


_TOKEN = re.compile(
    r"\x1b\[<(?P<mb>\d+);(?P<mx>\d+);(?P<my>\d+)(?P<mf>[Mm])"
    r"|\x1b\[(?P<cp>[0-9;]*)(?P<cf>[A-Za-z~])"
    r"|\x1bO(?P<sf>[A-Za-z])"
    r"|(?P<esc>\x1b)"
    r"|(?P<crlf>\r\n)"
    r"|(?P<ch>[\s\S])")
_ARROWS = {"A": "up", "B": "down", "C": "right", "D": "left",
           "H": "home", "F": "end", "Z": "backtab"}
_TILDE = {"1": "home", "7": "home", "4": "end", "8": "end", "3": "delete"}
_CTRLKEYS = {"\t": "tab", "\r": "enter", "\n": "enter", "\x7f": "backspace",
             "\x08": "backspace", "\x01": "home", "\x05": "end", "\x15": "clear",
             "\x19": "copy", "\x03": "quit"}


def parse_input(data):
    ev, text = [], []

    def flush():
        if text:
            ev.append(("text", "".join(text)))
            text.clear()

    for m in _TOKEN.finditer(data):
        if m["mb"] is not None:
            flush()
            if m["mf"] == "M" and int(m["mb"]) < 32 and int(m["mb"]) & 3 == 0:
                ev.append(("click", int(m["mx"]) - 1, int(m["my"]) - 1))
        elif m["cf"] is not None:
            flush()
            k = (_TILDE.get(m["cp"].split(";")[0]) if m["cf"] == "~"
                 else _ARROWS.get(m["cf"]))
            if k:
                ev.append(("key", k))
        elif m["sf"] is not None:
            flush()
            k = _ARROWS.get(m["sf"])
            if k:
                ev.append(("key", k))
        elif m["esc"]:
            flush()
        elif m["crlf"]:
            flush()
            ev.append(("key", "enter"))
        else:
            ch = m["ch"]
            if ch in _CTRLKEYS:
                flush()
                ev.append(("key", _CTRLKEYS[ch]))
            elif ch >= " " and ch != "\x7f":
                text.append(ch)
    flush()
    return ev


# -------------------------------------------------------------- helpers
def clean_path(raw):
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1]
    if s.startswith("file://"):
        s = unquote(s[7:])
        if IS_WINDOWS and re.match(r"^/[A-Za-z]:", s):
            s = s[1:]
    if not IS_WINDOWS:
        s = s.replace("\\ ", " ")
    return Path(os.path.expanduser(s))


def local_url(raw):
    raw = raw.strip()
    if re.fullmatch(r"\d{1,5}", raw):
        return f"http://127.0.0.1:{raw}"
    if re.match(r"https?://", raw):
        return raw
    if re.fullmatch(r"[\w.\-]+:\d{1,5}", raw):
        return "http://" + raw
    return None


# -------------------------------------------------------------------- app
class App:
    def __init__(self, html, port):
        self.term = Term()
        self.f = {"html": Field(html or ""),
                  "port": Field(str(port) if port else ""),
                  "tun": Field("")}
        self.focus = "html"
        self.split = False
        self.msg = {}
        self.regions = []
        self.m1 = Module()
        self.m2 = Module(PUBLIC_RE)

    # ---- actions
    def order(self):
        return ["html", "port"] + (["tun"] if self.split else [])

    def open_split(self):
        if not self.split:
            self.split = True
            if not self.f["tun"].text:
                self.f["tun"] = Field(self.f["port"].text or "8000")
        self.focus = "tun"

    def move(self, step):
        o = self.order()
        i = o.index(self.focus) if self.focus in o else 0
        self.focus = o[(i + step) % len(o)]

    def start_backend(self):
        p = clean_path(self.f["html"].text)
        if not p.is_file():
            self.msg["html"] = ("x file not found", False)
            self.focus = "html"
            return
        self.msg["html"] = ("ok file found", True)
        raw = self.f["port"].text.strip() or "8000"
        port = int(raw)
        if not 1 <= port <= 65535:
            self.msg["port"] = ("x port must be 1-65535", False)
            return
        if not BACKEND_SCRIPT.exists():
            self.msg["port"] = ("x backen2.py not found next to IQchain.py", False)
            return
        self.m1.target = f"http://127.0.0.1:{port}"
        self.m1.start([sys.executable, "-u", str(BACKEND_SCRIPT), str(p.resolve()),
                       "--port", str(port)])
        self.msg["port"] = (f"ok starting on {port}", True)

    def start_tunnel(self):
        raw = self.f["tun"].text.strip() or self.f["port"].text.strip() or "8000"
        url = local_url(raw)
        if not url:
            self.msg["tun"] = ("x type a port, e.g. 8000", False)
            return
        if not TUNNEL_SCRIPT.exists():
            self.msg["tun"] = ("x tunnel.py not found next to IQchain.py", False)
            return
        self.m2.target = url
        self.m2.start([sys.executable, "-u", str(TUNNEL_SCRIPT), url])
        self.msg["tun"] = ("ok starting tunnel", True)

    def enter(self):
        if self.focus == "html":
            if clean_path(self.f["html"].text).is_file():
                self.msg["html"] = ("ok file found", True)
                self.focus = "port"
            else:
                self.msg["html"] = ("x file not found", False)
        elif self.focus == "port":
            self.start_backend()
        else:
            self.start_tunnel()

    def copy_public(self):
        if self.m2.pin:
            b = base64.b64encode(self.m2.pin.encode()).decode()
            sys.stdout.write(f"{ESC}]52;c;{b}\a")
            sys.stdout.flush()
            self.msg["tun"] = ("ok public link copied", True)

    def handle(self, ev):
        kind = ev[0]
        f = self.f[self.focus]
        if kind == "click":
            for (x0, y0, x1, y1, act) in reversed(self.regions):
                if x0 <= ev[1] <= x1 and y0 <= ev[2] <= y1:
                    if act[0] == "split":
                        self.open_split()
                    else:
                        self.focus = act[1]
                        self.f[act[1]].end()
                    break
        elif kind == "text":
            t = ev[1]
            if t == "+" and self.focus != "html" and not self.split:
                self.open_split()
                return
            if self.focus == "port":
                t = "".join(ch for ch in t if ch.isdigit())
            f.insert(t)
            self.msg.pop(self.focus, None)
        else:
            k = ev[1]
            if k == "quit":
                return "quit"
            if k == "enter":
                self.enter()
            elif k == "tab":
                self.open_split() if not self.split else self.move(1)
            elif k == "backtab":
                self.move(-1)
            elif k == "down":
                self.move(1)
            elif k == "up":
                self.move(-1)
            elif k == "copy":
                self.copy_public()
            else:
                getattr(f, {"backspace": "backspace", "delete": "delete",
                            "left": "left", "right": "right", "home": "home",
                            "end": "end", "clear": "clear"}[k])()

    # ---- drawing
    def field(self, c, name, x, y, w, placeholder):
        fd, foc = self.f[name], self.focus == name
        c.box(x, y, x + w - 1, y + 2, ORANGE if foc else GREY)
        iw = w - 4
        start = max(0, fd.cur - iw + 1)
        if fd.text:
            c.put(x + 2, y + 1, fd.text[start:start + iw], WHITE, x + w - 3)
        else:
            c.put(x + 3, y + 1, placeholder[:iw - 1], GREY, x + w - 3)
        if foc:
            cx = x + 2 + fd.cur - start
            ch = fd.text[fd.cur] if fd.cur < len(fd.text) else " "
            c.put(cx, y + 1, ch, REV, x + w - 3)
        self.regions.append((x, y, x + w - 1, y + 2, ("focus", name)))

    def label(self, c, name, x0, x1, y, text):
        c.put(x0, y, text, BOLD + WHITE, x1)
        m = self.msg.get(name)
        if m:
            t, ok = m
            c.put(x0 + len(text) + 2, y, ("✓ " if ok else "✗ ") + t[3 if ok else 2:],
                  GREEN if ok else RED, x1)

    def log(self, c, mod, x0, x1, y, y1):
        if y > y1:
            return
        t = "─ output "
        c.put(x0, y, t + "─" * (x1 - x0 + 1 - len(t)), GREY, x1)
        n = y1 - y
        for i, line in enumerate(list(mod.lines)[-n:] if n > 0 else []):
            c.put(x0, y + 1 + i, line, GREY, x1)

    def pane1(self, c, x0, x1, y0, y1):
        fw = min(x1 - x0 + 1, 64)
        self.label(c, "html", x0, x1, y0, "* HTML file path")
        self.field(c, "html", x0, y0 + 1, fw, "e.g. /a/b/c/index.html")
        self.label(c, "port", x0, x1, y0 + 4, "* Local Host port")
        self.field(c, "port", x0, y0 + 5, fw, "e.g. 8000   (just Enter = 8000)")
        if self.m1.running:
            c.put(x0, y0 + 8, "● Local Host : " + self.m1.target, GREEN, x1)
        elif self.m1.proc is not None:
            c.put(x0, y0 + 8, "■ backend stopped", RED, x1)
        else:
            c.put(x0, y0 + 8, "○ Local Host : not started", GREY, x1)
        self.log(c, self.m1, x0, x1, y0 + 9, y1)

    def pane2(self, c, x0, x1, y0, y1):
        fw = min(x1 - x0 + 1, 64)
        self.label(c, "tun", x0, x1, y0, "* feed the local host")
        self.field(c, "tun", x0, y0 + 1, fw, "e.g. 8000")
        m = self.m2
        live = bool(m.pin and m.running)
        if live:
            c.put(x0, y0 + 4, "● tunnel running", GREEN, x1)
        elif m.running:
            c.put(x0, y0 + 4, "◌ connecting ...", ORANGE, x1)
        elif m.proc is not None:
            c.put(x0, y0 + 4, "■ tunnel stopped", RED, x1)
        else:
            c.put(x0, y0 + 4, "○ press Enter to start the tunnel", GREY, x1)
        by, bw = y0 + 5, min(x1 - x0 + 1, 72)
        avail = max(bw - 4 - 13, 10)
        pub = m.pin or "-"
        chunks = [pub[i:i + avail] for i in range(0, len(pub), avail)]
        h = 3 + len(chunks)
        st = ORANGE if live else GREY
        c.box(x0, by, x0 + bw - 1, by + h - 1, st,
              " TUNNEL ON LIVE " if live else " TUNNEL ")
        c.put(x0 + 2, by + 1, "Local Host : " + (m.target or "-"), WHITE, x1)
        for i, ch in enumerate(chunks):
            c.put(x0 + 2, by + 2 + i, ("Public     : " if i == 0 else " " * 13) + ch,
                  BOLD + GREEN if live else GREY, x1)
        self.log(c, m, x0, x1, by + h, y1)

    def build(self, W, H):
        c = Canvas(W, H)
        self.regions = []
        c.box(0, 0, W - 1, H - 1, GREY)
        if self.m2.pin and self.m2.running:
            status = "( tunnel live )"
        elif self.m1.running:
            status = "( backend live )"
        else:
            status = "( hang on )"
        c.center(1, status, ORANGE)
        big = H >= 26
        if big:
            for r, line in enumerate(LOGO):
                c.center(2 + r, line, BOLD + ORANGE)
            ybtn, logo_h = 6, 5
        else:
            c.center(2, "I Q c h a i n", BOLD + ORANGE)
            ybtn, logo_h = 2, 1
        c.put(3, ybtn, "[ + ]", GREY if self.split else BOLD + ORANGE)
        self.regions.append((3, ybtn, 7, ybtn, ("split",)))
        c.put(W - 3 - len(VERSION_TAG), 2 + logo_h, VERSION_TAG, GREY)
        yd1, yh, yd2 = 3 + logo_h, 4 + logo_h, 5 + logo_h
        top, bot = yd2 + 1, H - 4
        mid = W // 2
        for y, l, m_, r in ((yd1, "├", "┬", "┤"), (yd2, "├", "┼", "┤"),
                            (H - 3, "├", "┴", "┤")):
            c.put(0, y, l + "─" * (W - 2) + r, GREY)
            if self.split:
                c.put(mid, y, m_, GREY)
        if self.split:
            for y in [yh] + list(range(top, H - 3)):
                c.put(mid, y, "│", GREY)
            l0, l1, r0, r1 = 2, mid - 2, mid + 2, W - 3
        else:
            l0, l1 = 2, W - 3
        hl = BOLD + (ORANGE if self.focus in ("html", "port") else WHITE)
        t1 = "MODULE - 1 (backend)"
        c.put((l0 + l1 - len(t1)) // 2 + 1, yh, t1, hl)
        self.pane1(c, l0, l1, top, bot)
        if self.split:
            t2 = "MODULE - 2 (tunnel)"
            c.put((r0 + r1 - len(t2)) // 2 + 1, yh, t2,
                  BOLD + (ORANGE if self.focus == "tun" else WHITE))
            self.pane2(c, r0, r1, top, bot)
        hint = ("Tab / [ + ] open tunnel   Up/Down move   Enter confirm   Ctrl+C quit"
                if not self.split else
                "Tab next box   Enter start   Ctrl+Y copy public link   Ctrl+C stop all")
        c.put(2, H - 2, hint, ORANGE)
        return c.render()

    # ---- main loop
    def run(self):
        self.term.open()
        size, last = None, None
        try:
            while True:
                W, H = self.term.size()
                if (W, H) != size:
                    sys.stdout.write(f"{ESC}[2J")
                    size, last = (W, H), None
                frame = self.build(W, H)
                if frame != last:
                    sys.stdout.write(f"{ESC}[H" + frame)
                    sys.stdout.flush()
                    last = frame
                data = self.term.read(0.1)
                for ev in parse_input(data) if data else ():
                    if self.handle(ev) == "quit":
                        return
        except KeyboardInterrupt:
            pass
        finally:
            self.m2.stop()
            self.m1.stop()
            self.term.close()


def main():
    ap = argparse.ArgumentParser(description="IQchain - one frame, two live modules")
    ap.add_argument("html", nargs="?", help="(optional) HTML file path")
    ap.add_argument("--port", type=int, help="(optional) local port")
    a = ap.parse_args()
    if not sys.stdin.isatty():
        sys.exit("IQchain needs a real terminal.")
    App(a.html, a.port).run()


if __name__ == "__main__":
    main()
