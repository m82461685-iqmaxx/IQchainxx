#!/usr/bin/env python3
"""iqfix - inspect a terminal Python script and rebuild it as a single file:

    one self-contained file (plus a text report). Inside it the design block
    (colors, banners, glyphs) is separated from the responsive layout engine
    (zoom, resize, wrap, safe input, frame fitting) and the original logic.
    one output file containing: layout engine + design + original logic

Standard library only. Works on Termux, Linux terminals and PowerShell.
Run:  python iqfix.py
"""
import ast
import builtins
import os
import re
import sys
import types

# --------------------------------------------------------------------------
# layout.py source (written to the output folder, also used by this tool)
# --------------------------------------------------------------------------
LAYOUT_SRC = r'''# -*- coding: utf-8 -*-
"""layout.py - responsive terminal layout engine. Standard library only.

install()  : wraps stdout so every printed line always fits the terminal width,
             re-flows the screen on resize / zoom, and fixes line input.
ask()      : safe input() (arrow keys, ANSI prompts, tab path completion).
clear()    : clear screen and reset the replay history.
"""
import atexit
import builtins
import glob
import os
import re
import shutil
import signal
import sys
import threading
import time
import unicodedata

try:
    import readline
except Exception:
    readline = None

TOKEN = re.compile(
    r"(\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9])")
SGR_RESET = re.compile(r"\x1b\[0?m")
CLEAR = re.compile(r"\x1b\[[23]?J|\x1bc")
PASS = re.compile(r"\x1b\[[0-9;]*[ABCDEFGHfsu]|\x1b[78]|\r")
CTRL = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1bO[A-Za-z]|[\x00-\x08\x0b-\x1f\x7f]")

HCH = set("\u2500\u2501\u2550\u2504\u2505\u2508\u2509\u254c\u254d-=_~")
VCH = set("\u2502\u2503\u2551\u2506\u2507\u250a\u250b|")
TOPL = set("\u250c\u250f\u2554\u256d\u250d\u250e\u2552\u2553+")
TOPR = set("\u2510\u2513\u2557\u256e\u2511\u2512\u2555\u2556+")
BOTL = set("\u2514\u2517\u255a\u2570\u2515\u2516\u2558\u2559+")
BOTR = set("\u2518\u251b\u255d\u256f\u2519\u251a\u255b\u255c+")


# ---------------------------------------------------------------- measuring
def cw(ch):
    o = ord(ch)
    if o < 32 or 0x7F <= o < 0xA0:
        return 0
    cat = unicodedata.category(ch)
    if cat in ("Mn", "Me", "Cf"):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def strip_ansi(s):
    return TOKEN.sub("", s)


def dw(s):
    return sum(cw(c) for c in strip_ansi(s))


def size():
    cols = rows = 0
    try:
        fd = sys.__stdout__.fileno()
        sz = os.get_terminal_size(fd)
        cols, rows = sz.columns, sz.lines
    except Exception:
        try:
            cols, rows = shutil.get_terminal_size((80, 24))
        except Exception:
            cols, rows = 80, 24
    if cols <= 0:
        cols = 80
    if rows <= 0:
        rows = 24
    return max(cols, 10), max(rows, 4)


def width():
    return size()[0]


def get_terminal_size(fallback=(80, 24)):
    c, r = size()
    return os.terminal_size((c, r))


def hr(ch="\u2500", cap=None):
    w = width()
    return ch * (min(w, cap) if cap else w)


def center(text):
    return " " * max(0, (width() - dw(text)) // 2) + text


# ---------------------------------------------------------------- tokens
def _items(line):
    out = []
    for i, p in enumerate(TOKEN.split(line)):
        if i % 2:
            out.append(("a", p))
        else:
            out.extend(("c", c) for c in p)
    return out


def _join(items):
    return "".join(t for _, t in items)


def _iw(items):
    return sum(cw(t) for k, t in items if k == "c")


def _sgr_update(st, t):
    if SGR_RESET.fullmatch(t):
        return []
    if t.endswith("m"):
        return st + [t]
    return st


# ---------------------------------------------------------------- fitting
def shrink(line, w):
    """Remove padding (spaces / rule characters) until the line fits."""
    items = _items(line)
    excess = _iw(items) - w
    while excess > 0:
        best = None
        i, n = 0, len(items)
        while i < n:
            k, t = items[i]
            if k == "c" and (t == " " or t in HCH):
                j = i
                while j < n and items[j] == (k, t):
                    j += 1
                if j - i >= 2 and (best is None or j - i > best[1]):
                    best = (i, j - i)
                i = j
            else:
                i += 1
        if best is None:
            break
        del items[best[0]]
        excess -= 1
    return _join(items), excess <= 0


def wrap(line, w):
    """ANSI-aware word wrap that keeps indentation and color state."""
    if dw(line) <= w:
        return [line]
    w = max(w, 1)
    items = _items(line)
    indent = 0
    for k, t in items:
        if k == "a":
            continue
        if t == " ":
            indent += 1
        else:
            break
    if indent >= w // 2:
        indent = 0
    out, cur, snap, st = [], [], [], []
    lead, first, sp, vis = "", True, -1, False
    for it in items:
        k, t = it
        if k == "a":
            st = _sgr_update(st, t)
            cur.append(it)
            snap.append(st)
            continue
        limit = w if first else w - indent
        if _iw(cur) + cw(t) > limit and vis:
            if sp > 0:
                chunk, es = cur[:sp], snap[sp - 1]
                rest, rsnap = cur[sp + 1:], snap[sp + 1:]
            else:
                chunk, es = cur, (snap[-1] if snap else st)
                rest, rsnap = [], []
            out.append(lead + ("" if first else " " * indent) + _join(chunk)
                       + ("\x1b[0m" if es else ""))
            lead = "".join(es)
            first = False
            cur, snap = rest, rsnap
            sp, vis = -1, False
            for idx, (kk, tt) in enumerate(cur):
                if kk == "c":
                    if tt == " ":
                        if vis:
                            sp = idx
                    else:
                        vis = True
        if t == " " and vis:
            sp = len(cur)
        if t != " ":
            vis = True
        cur.append(it)
        snap.append(st)
    out.append(lead + ("" if first else " " * indent) + _join(cur))
    return out


def _is_rule(p):
    b = p.strip()
    return len(b) >= 3 and all(c in HCH for c in b)


def _is_frame(p):
    b = p.strip()
    return (len(b) >= 2 and b[0] in (VCH | TOPL | BOTL)
            and b[-1] in (VCH | TOPR | BOTR))


def fit_line(line, w):
    if dw(line) <= w:
        return [line]
    p = strip_ansi(line)
    if _is_rule(p) or _is_frame(p):
        s, ok = shrink(line, w)
        if ok:
            return [s]
    return wrap(line, w)


def _is_top(line):
    b = strip_ansi(line).strip()
    if len(b) < 4 or b[0] not in TOPL or b[-1] not in TOPR:
        return False
    inner = b[1:-1]
    return sum(c in HCH for c in inner) * 10 >= len(inner) * 6


def _is_bottom(line):
    b = strip_ansi(line).strip()
    if len(b) < 4 or b[0] not in BOTL or b[-1] not in BOTR:
        return False
    inner = b[1:-1]
    return sum(c in HCH for c in inner) * 10 >= len(inner) * 6


def _lead_sgr(line):
    st = []
    for k, t in _items(line):
        if k == "a":
            st = _sgr_update(st, t)
        elif t != " ":
            break
    return "".join(st)


def _text_sgr(line):
    st = []
    for k, t in _items(line):
        if k == "a":
            st = _sgr_update(st, t)
        elif t.isalnum():
            break
    return "".join(st)


def _compact(blk, w):
    """Fallback for a framed block that cannot shrink: rebuild a small frame
    that keeps only the readable text lines (art lines are dropped)."""
    if w < 8 or len(blk) < 3 or not _is_bottom(blk[-1]):
        return None
    plain = [strip_ansi(l).strip() for l in blk]
    top, bot = plain[0], plain[-1]
    tl, tr, bl, br = top[0], top[-1], bot[0], bot[-1]
    inner = top[1:-1]
    hs = [c for c in inner if c in HCH]
    hch = max(set(hs), key=hs.count) if hs else "-"
    vch = "|"
    for p in plain[1:-1]:
        if p and p[0] in VCH:
            vch = p[0]
            break
    fc = _lead_sgr(blk[0])
    rst = "\x1b[0m"
    texts = []
    for l, p in zip(blk[1:-1], plain[1:-1]):
        t = p.strip("".join(VCH) + " ")
        if re.search(r"[^\W\d_]{3,}", t):
            texts.append((t, _text_sgr(l)))
    if not texts:
        return None
    iw = w - 4
    edge = (fc + vch + rst) if fc else vch

    def row(content, sgr=""):
        pad = iw - dw(content)
        left = pad // 2
        body = (sgr + content + rst) if sgr else content
        return edge + " " + " " * left + body + " " * (pad - left) + " " + edge

    def border(a, b):
        s = a + hch * (w - 2) + b
        return (fc + s + rst) if fc else s

    rows = [border(tl, tr), row("")]
    for t, sgr in texts:
        for piece in wrap(t, iw):
            rows.append(row(piece, sgr))
    rows += [row(""), border(bl, br)]
    return rows


class Machine:
    """Turns raw lines into lines that fit width w. Buffers framed blocks."""

    def __init__(self, w):
        self.w = w
        self.blk = None

    def feed(self, line):
        if "\t" in line:
            line = line.expandtabs(8)
        if self.blk is not None:
            self.blk.append(line)
            if _is_bottom(line) or len(self.blk) > 60:
                return self._close()
            return []
        if _is_top(line):
            self.blk = [line]
            return []
        return fit_line(line, self.w)

    def end(self):
        return self._close() if self.blk else []

    def _close(self):
        blk, self.blk, w = self.blk, None, self.w
        if max(dw(l) for l in blk) <= w:
            return blk
        out, ok = [], True
        for l in blk:
            if dw(l) <= w:
                out.append(l)
                continue
            s, good = shrink(l, w)
            if not good:
                ok = False
                break
            out.append(s)
        if ok:
            return out
        c = _compact(blk, w)
        if c:
            return c
        res = []
        for l in blk:
            res.extend(fit_line(l, w))
        return res


# ---------------------------------------------------------------- stdout wrapper
class Fitter:
    def __init__(self, real):
        self.real = real
        self.buf = ""
        self.hist = []
        self.w = width()
        self.m = Machine(self.w)
        self.lock = threading.RLock()
        self.prompt = None

    def __getattr__(self, name):
        return getattr(self.real, name)

    def _emit(self, lines):
        for l in lines:
            self.real.write(l + "\n")

    def write(self, s):
        if not isinstance(s, str):
            s = str(s)
        if not s:
            return 0
        with self.lock:
            if CLEAR.search(s):
                self.hist = []
            self.buf += s
            if "\n" not in self.buf and self.m.blk is None and CLEAR.search(self.buf):
                self.real.write(self.buf)
                self.buf = ""
                self.real.flush()
                return len(s)
            if "\n" in self.buf:
                parts = self.buf.split("\n")
                self.buf = parts.pop()
                for l in parts:
                    l = l.rstrip("\r")
                    if PASS.search(l):
                        self._emit(self.m.end())
                        self.real.write(l + "\n")
                        continue
                    self.hist.append(CLEAR.sub("", l))
                    self._emit(self.m.feed(l))
                if len(self.hist) > 3000:
                    del self.hist[:1000]
        return len(s)

    def writelines(self, lines):
        for l in lines:
            self.write(l)

    def flush(self):
        with self.lock:
            self._emit(self.m.end())
            if self.buf:
                self.real.write(self.buf)
                self.buf = ""
            self.real.flush()

    def relayout(self):
        with self.lock:
            nw = width()
            if nw == self.w:
                return
            self.w = nw
            m = Machine(nw)
            out = []
            for l in self.hist:
                out.extend(m.feed(l))
            out.extend(m.end())
            rows = size()[1]
            tail = out[-(rows - 1):] if rows > 1 else out
            self.m = Machine(nw)
            self.real.write("\x1b[2J\x1b[H")
            for l in tail:
                self.real.write(l + "\n")
            if self.prompt is not None:
                typed = ""
                if readline is not None:
                    try:
                        typed = readline.get_line_buffer()
                    except Exception:
                        typed = ""
                self.real.write(self.prompt + typed)
            elif self.buf:
                self.real.write(self.buf)
            self.real.flush()


_F = None


def _clean(s):
    return CTRL.sub("", s)


def _complete(text, state):
    try:
        p = os.path.expanduser(text)
        m = sorted(glob.glob(p + "*"))
        m = [x + ("/" if os.path.isdir(x) else "") for x in m]
        return m[state] if state < len(m) else None
    except Exception:
        return None


def _setup_readline():
    if readline is None:
        return
    try:
        readline.set_completer_delims(" \t\n;")
        readline.set_completer(_complete)
        if "libedit" in (readline.__doc__ or ""):
            readline.parse_and_bind("bind ^I rl_complete")
        else:
            readline.parse_and_bind("tab: complete")
    except Exception:
        pass


def _enable_vt():
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        for hid in (-11, -12):
            h = k.GetStdHandle(hid)
            m = ctypes.c_uint32()
            if k.GetConsoleMode(h, ctypes.byref(m)):
                k.SetConsoleMode(h, m.value | 4)
    except Exception:
        pass


def _utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _poll():
    while True:
        time.sleep(0.3)
        try:
            if _F is not None and _F.prompt is None and width() != _F.w:
                _F.relayout()
        except Exception:
            pass


def _shutdown():
    try:
        if _F is not None:
            _F.flush()
    except Exception:
        pass


def install():
    """Activate responsive output. Safe to call more than once."""
    global _F
    if _F is not None:
        return
    _enable_vt()
    _utf8()
    try:
        if not sys.stdout.isatty():
            return
    except Exception:
        return
    _setup_readline()
    _F = Fitter(sys.stdout)
    sys.stdout = _F
    atexit.register(_shutdown)
    if hasattr(signal, "SIGWINCH"):
        signal.signal(signal.SIGWINCH, lambda s, f: _F.relayout())
    else:
        threading.Thread(target=_poll, daemon=True).start()


def clear():
    f = _F
    if f is not None:
        f.flush()
        f.hist = []
        f.real.write("\x1b[2J\x1b[H")
        f.real.flush()
    else:
        sys.stdout.write("\x1b[2J\x1b[H")
        sys.stdout.flush()


def ask(prompt=""):
    """Replacement for input(): cursor keys, ANSI prompts, long prompts."""
    f = _F
    text = str(prompt)
    if f is None:
        return _clean(builtins.input(text))
    with f.lock:
        text = f.buf + text
        f.buf = ""
        head, _, last = text.rpartition("\n")
        if "\n" in text:
            f.write(head + "\n")
        f._emit(f.m.end())
        w = width()
        if dw(last) > w - 1:
            pieces = wrap(last, w - 1)
            for pc in pieces[:-1]:
                f.write(pc + "\n")
            last = pieces[-1]
        rp = TOKEN.sub(lambda m: "\x01" + m.group(0) + "\x02", last) if readline else last
        f.prompt = last
    sys.stdout = f.real
    try:
        s = builtins.input(rp)
    finally:
        sys.stdout = f
        f.prompt = None
    f.hist.append(last + s)
    return _clean(s)
'''

# --------------------------------------------------------------------------
# terminal styling for this tool
# --------------------------------------------------------------------------
ORANGE = "\x1b[38;5;208m"
GREEN = "\x1b[32m"
RED = "\x1b[31m"
YELLOW = "\x1b[33m"
DIM = "\x1b[90m"
BOLD = "\x1b[1m"
RST = "\x1b[0m"

L = None  # layout module, loaded at start


def load_layout():
    mod = types.ModuleType("layout")
    exec(compile(LAYOUT_SRC, "layout.py", "exec"), mod.__dict__)
    return mod


# --------------------------------------------------------------------------
# analysis and splitting
# --------------------------------------------------------------------------
PURE = (ast.Constant, ast.JoinedStr, ast.FormattedValue, ast.BinOp, ast.UnaryOp,
        ast.Tuple, ast.List, ast.Dict, ast.Set, ast.Name, ast.Subscript, ast.Slice,
        ast.operator, ast.unaryop, ast.expr_context)

DESIGN_TOKENS = {
    "color", "colors", "colour", "colours", "theme", "banner", "logo", "art", "ascii",
    "style", "styles", "palette", "border", "box", "glyph", "glyphs", "icon", "icons",
    "symbol", "symbols", "frame", "title", "header", "footer", "reset", "bold", "dim",
    "ansi", "fg", "bg", "rule", "divider", "red", "green", "blue", "yellow", "cyan",
    "magenta", "orange", "white", "gray", "grey", "black", "accent", "brand", "tagline",
    "subtitle", "spinner", "bullet", "arrow", "tick", "cross",
}

CLEAR_CMDS = ("clear", "cls")


def name_tokens(name):
    parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", name)
    return {p.lower() for p in parts}


def assign_info(n):
    if isinstance(n, ast.Assign) and all(isinstance(t, ast.Name) for t in n.targets):
        return [t.id for t in n.targets], n.value
    if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.value is not None:
        return [n.target.id], n.value
    return None


SAFE_METHODS = {"ljust", "rjust", "center", "join", "format", "upper", "lower", "strip",
                "lstrip", "rstrip", "title", "replace", "zfill", "capitalize", "split"}
SAFE_FUNCS = {"len", "str", "chr", "min", "max", "int", "tuple", "list"}


def is_pure(v):
    for x in ast.walk(v):
        if isinstance(x, ast.Call):
            f = x.func
            if isinstance(f, ast.Attribute) and f.attr in SAFE_METHODS:
                continue
            if isinstance(f, ast.Name) and f.id in SAFE_FUNCS:
                continue
            return False
        if isinstance(x, (ast.Attribute, ast.keyword)):
            if isinstance(x, ast.Attribute) and x.attr not in SAFE_METHODS:
                return False
            continue
        if not isinstance(x, PURE):
            return False
    return True


def loaded_names(v):
    return {x.id for x in ast.walk(v) if isinstance(x, ast.Name)}


def looks_design(names, value):
    toks = set()
    for nm in names:
        toks |= name_tokens(nm)
    if toks & DESIGN_TOKENS:
        return True
    for x in ast.walk(value):
        if isinstance(x, ast.Constant) and isinstance(x.value, str):
            s = x.value
            if "\x1b" in s or any("\u2500" <= c <= "\u259f" for c in s):
                return True
            if "\n" in s and len(s) > 40:
                body = s.replace("\n", "").replace(" ", "")
                sym = sum(1 for c in body if not c.isalnum())
                if body and sym * 2 >= len(body):
                    return True
    return False


def process(src, stem):
    src = src.replace("\r\n", "\n").replace("\r", "\n")
    data = src.encode("utf-8")
    starts = [0] + [m.end() for m in re.finditer(b"\n", data)]

    def pos(ln, col):
        return starts[ln - 1] + col

    def line_end(ln):
        return starts[ln] if ln < len(starts) else len(data)

    tree = ast.parse(src)
    body = tree.body
    builtin_names = set(dir(builtins))
    top_defs = {n.name for n in body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}

    edits = []
    chunks = []
    design_names = set()
    moved = []

    for node in body:
        info = assign_info(node)
        if info and is_pure(info[1]):
            names, value = info
            if looks_design(names, value) and loaded_names(value) <= (design_names | builtin_names):
                design_names |= set(names)
                moved.extend(names)
                chunks.append(data[starts[node.lineno - 1]:line_end(node.end_lineno)].decode("utf-8"))
                edits.append((starts[node.lineno - 1], line_end(node.end_lineno), b""))
            continue
        if (isinstance(node, ast.ClassDef) and not node.decorator_list and not node.keywords
                and all(isinstance(b, ast.Name) and b.id == "object" for b in node.bases)):
            ok, local, anyd = True, set(), False
            for st in node.body:
                if isinstance(st, ast.Pass):
                    continue
                if (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant)
                        and isinstance(st.value.value, str)):
                    continue
                ai = assign_info(st)
                if not ai or not is_pure(ai[1]) or not (loaded_names(ai[1]) <= (design_names | local | builtin_names)):
                    ok = False
                    break
                local |= set(ai[0])
                anyd = anyd or looks_design(ai[0], ai[1])
            if ok and local and (anyd or (name_tokens(node.name) & DESIGN_TOKENS)):
                design_names.add(node.name)
                moved.append(node.name)
                chunks.append(data[starts[node.lineno - 1]:line_end(node.end_lineno)].decode("utf-8"))
                edits.append((starts[node.lineno - 1], line_end(node.end_lineno), b""))

    # findings and call patches
    findings = {"input": [], "clear": [], "termsize": [], "fixed_width": [],
                "tamil": [], "external": []}
    stdlib = getattr(sys, "stdlib_module_names", None)
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name) and f.id == "input" and "input" not in top_defs:
                findings["input"].append(n.lineno)
                edits.append((pos(f.lineno, f.col_offset), pos(f.end_lineno, f.end_col_offset), b"layout.ask"))
            elif (isinstance(f, ast.Attribute) and f.attr == "system" and isinstance(f.value, ast.Name)
                  and f.value.id == "os" and n.args and isinstance(n.args[0], ast.Constant)
                  and n.args[0].value in CLEAR_CMDS):
                findings["clear"].append(n.lineno)
                edits.append((pos(n.lineno, n.col_offset), pos(n.end_lineno, n.end_col_offset), b"layout.clear()"))
            elif isinstance(f, ast.Attribute) and f.attr == "get_terminal_size":
                findings["termsize"].append(n.lineno)
                edits.append((pos(f.lineno, f.col_offset), pos(f.end_lineno, f.end_col_offset), b"layout.get_terminal_size"))
            elif (isinstance(f, ast.Attribute) and f.attr in ("center", "ljust", "rjust") and n.args
                  and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, int)
                  and n.args[0].value >= 20):
                findings["fixed_width"].append(n.lineno)
        elif isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mult):
            a, b = n.left, n.right
            for s, k in ((a, b), (b, a)):
                if (isinstance(s, ast.Constant) and isinstance(s.value, str)
                        and isinstance(k, ast.Constant) and isinstance(k.value, int) and k.value >= 20):
                    findings["fixed_width"].append(n.lineno)
                    break
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in n.names] if isinstance(n, ast.Import) else (
                [n.module] if n.module and n.level == 0 else [])
            for m in mods:
                root = m.split(".")[0]
                if stdlib is not None and root not in stdlib and root not in ("design", "layout"):
                    findings["external"].append((n.lineno, root))
    for i, line in enumerate(src.split("\n"), 1):
        if any("\u0b80" <= c <= "\u0bff" for c in line):
            findings["tamil"].append(i)

    # header insertion (after docstring and __future__ imports)
    idx = 0
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        idx = 1
    while idx < len(body) and isinstance(body[idx], ast.ImportFrom) and body[idx].module == "__future__":
        idx += 1
    if idx < len(body):
        first = body[idx]
        ln = min([first.lineno] + [d.lineno for d in getattr(first, "decorator_list", [])])
        ins = starts[ln - 1]
    else:
        ins = len(data)
    parts = [
        "# " + "=" * 66,
        "# LAYOUT ENGINE (auto-generated by iqfix): zoom, resize, wrap, safe input",
        "# " + "=" * 66,
        "import types as _types",
        "_LAYOUT_SRC = r\'\'\'" + LAYOUT_SRC + "\'\'\'",
        "layout = _types.ModuleType('layout')",
        "exec(compile(_LAYOUT_SRC, '<layout>', 'exec'), layout.__dict__)",
        "layout.install()",
        "",
        "# " + "=" * 66,
        "# DESIGN: colors, glyphs, banners, geometry (moved out of the logic)",
        "# " + "=" * 66,
    ]
    if chunks:
        parts.append("\n\n".join(c.rstrip("\n") for c in chunks))
    parts += ["", "# " + "=" * 66, "# ORIGINAL LOGIC", "# " + "=" * 66]
    edits.append((ins, ins, ("\n".join(parts) + "\n\n").encode("utf-8")))

    out = bytearray(data)
    for s, e, rep in sorted(edits, key=lambda x: (x[0], x[1]), reverse=True):
        out[s:e] = rep
    main_src = out.decode("utf-8")

    design_src = ('# -*- coding: utf-8 -*-\n'
                  '"""Design layer: colors, glyphs, banners and geometry constants.\n\n'
                  'Test copy only."""\n\n')
    design_src += "\n\n".join(c.rstrip("\n") for c in chunks) + "\n\n"
    design_src += "__all__ = %r\n" % (moved,)
    return main_src, design_src, moved, findings


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------
def collect_strings(ns, names):
    found = []

    def walk(v):
        if isinstance(v, str):
            if "\n" in v or len(v) >= 12:
                found.append(v)
        elif isinstance(v, (list, tuple, set)):
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, type):
            for x in vars(v).values():
                walk(x)

    for nm in names:
        if nm in ns:
            walk(ns[nm])
    return found


def fit_test(lay, design_src, names):
    ns = {}
    exec(compile(design_src, "design.py", "exec"), ns)
    strings = collect_strings(ns, names)
    widths = (16, 20, 24, 28, 32, 40, 60, 80, 120)
    bad = []
    for w in widths:
        for s in strings:
            m = lay.Machine(w)
            out = []
            for line in s.split("\n"):
                out.extend(m.feed(line))
            out.extend(m.end())
            if any(lay.dw(x) > w for x in out):
                bad.append(w)
                break
    return len(strings), widths, bad


# --------------------------------------------------------------------------
# user interface
# --------------------------------------------------------------------------
def head(n, title):
    print()
    print("%s%sSTEP %d/3%s  %s%s%s" % (ORANGE, BOLD, n, RST, BOLD, title, RST))
    print(DIM + "-" * 34 + RST)


def banner():
    w = 46
    rows = [
        "\u250c" + "\u2500" * w + "\u2510",
        "\u2502" + "IQ-FIX  layout & design splitter".center(w) + "\u2502",
        "\u2502" + "stdlib only - zoom and resize safe".center(w) + "\u2502",
        "\u2514" + "\u2500" * w + "\u2518",
    ]
    for r in rows:
        print(ORANGE + r + RST)


def ask_file():
    while True:
        raw = L.ask("%sPython file path:%s " % (BOLD, RST)).strip().strip("\"'")
        if raw.lower() in ("q", "quit", "exit"):
            sys.exit(0)
        p = os.path.abspath(os.path.expanduser(raw))
        if os.path.isfile(p):
            return p
        print("%s  File not found: %s%s" % (RED, p, RST))


def read_text(path):
    for enc in ("utf-8-sig", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as fh:
                return fh.read()
        except UnicodeDecodeError:
            continue
    raise SystemExit("Cannot decode file")


def short(lines, n=6):
    lines = sorted(set(lines))
    s = ", ".join(str(x) for x in lines[:n])
    return s + (" ..." if len(lines) > n else "")


def main():
    global L
    L = load_layout()
    L.install()
    banner()

    head(1, "FRAMEWORK FILE PATH")
    src_path = ask_file()
    src = read_text(src_path)
    stem = os.path.splitext(os.path.basename(src_path))[0]
    try:
        main_src, design_src, moved, fnd = process(src, stem)
    except SyntaxError as exc:
        print("%s  Syntax error at line %s: %s%s" % (RED, exc.lineno, exc.msg, RST))
        sys.exit(1)

    print("%s  OK%s  parsed %d lines" % (GREEN, RST, src.count("\n") + 1))
    print("  design items moved : %d" % len(moved))
    print("  input() replaced   : %d" % len(fnd["input"]))
    print("  clear replaced     : %d" % len(fnd["clear"]))
    print("  terminal-size calls: %d" % len(fnd["termsize"]))
    if fnd["fixed_width"]:
        print("%s  fixed widths at lines %s (fitted at runtime)%s"
              % (YELLOW, short(fnd["fixed_width"]), RST))
    if fnd["tamil"]:
        print("%s  non-English text in code at lines %s%s" % (YELLOW, short(fnd["tamil"]), RST))
    ext = sorted({m for _, m in fnd["external"]})
    if ext:
        print("%s  external modules used: %s%s" % (YELLOW, ", ".join(ext), RST))

    head(2, "OUTPUT PATH (folder or .py file)")
    default = os.path.join(os.path.dirname(src_path), stem + "_fixed.py")
    raw = L.ask("%sSave result to%s [%s]: " % (BOLD, RST, default)).strip().strip("\"'")
    target = os.path.abspath(os.path.expanduser(raw)) if raw else default
    if not target.lower().endswith(".py"):
        target = os.path.join(target, stem + "_fixed.py")
    out_dir = os.path.dirname(target)
    os.makedirs(out_dir, exist_ok=True)
    if os.path.abspath(target) == os.path.abspath(src_path):
        target = os.path.join(out_dir, stem + "_fixed.py")

    head(3, "BUILD AND CHECK")
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(main_src)
    try:
        compile(main_src, target, "exec")
        print("%s  OK%s  output compiles" % (GREEN, RST))
    except SyntaxError as exc:
        print("%s  FAIL%s  output has an error at line %s: %s"
              % (RED, RST, exc.lineno, exc.msg))

    count, widths, bad = fit_test(L, design_src, moved)
    if not count:
        print("%s  --%s  no design strings found to test" % (YELLOW, RST))
    elif bad:
        print("%s  WARN%s  some strings overflow at widths: %s"
              % (YELLOW, RST, ", ".join(str(b) for b in sorted(set(bad)))))
    else:
        print("%s  OK%s  %d design strings fit at widths %d..%d"
              % (GREEN, RST, count, widths[0], widths[-1]))

    report = [
        "iqfix report for %s" % src_path,
        "design items moved: %s" % ", ".join(moved),
        "input() lines: %s" % short(fnd["input"], 50),
        "clear lines: %s" % short(fnd["clear"], 50),
        "terminal-size lines: %s" % short(fnd["termsize"], 50),
        "fixed-width lines: %s" % short(fnd["fixed_width"], 50),
        "non-English text lines: %s" % short(fnd["tamil"], 50),
        "external modules: %s" % ", ".join(ext),
    ]
    rep_path = os.path.splitext(target)[0] + "_report.txt"
    with open(rep_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(report) + "\n")

    print()
    print("%sFixed file:%s %s" % (BOLD, RST, target))
    print("%sReport    :%s %s" % (BOLD, RST, rep_path))
    print()
    print("Run it with:  python %s" % target)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
