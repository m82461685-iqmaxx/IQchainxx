#!/usr/bin/env python3
"""IQchain CLI logo - exact logo shape rendered with half-block pixels.
Works on Termux, Linux, macOS and Windows (Terminal / PowerShell / cmd). Pure stdlib.

    python iqchain_logo.py          # auto-picks size from terminal width
    python iqchain_logo.py small    # or: large
In code:  from iqchain_logo import show_logo; show_logo()
Env: NO_COLOR -> plain mono shape, IQ_256=1 -> force 256-colour mode.
"""
import os, sys, zlib, base64, shutil

LARGE = (96, 24, """
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
""")
SMALL = (46, 12, """
eNq1VXlM1EcUHm2t1daoraaJialpa2q8Aa8qKhUERBAEQZBbDrmXY9mVQwREBBERoXIoLAtFLSwiqyAgh+AucqnIsoB4BFFuVlhB
oHh8/TEmJBQT2zSdf37z3vve9/tm3swbQgjh+AaQiKhYkpx6kYSdiibqmrvJ+AgOCSdlt++gpq4JkuZHkDQ2Iyf/Fg5Y2tI4APJ/
DJMDFv8Ip29sjZbWDoy9eQtPTgAVkyPMxYB8ELJBoFraicddQxgf9x7LkJZVAkcWd4p2trcPcWV5kH3G5tQ2MDQlX8/9htjZ2X30
v96+QcT2kCsx2m9CTM0siZr6LmJn7zAFp6tnSHz8A4kLiz3Jv3DRD+R5Zx9kA8P4fNYCUnqrfFwP2nuG4Bt0Bpu3a2G9sgYcWFyk
ZuShoq4NNQ1tcPPypfnJ/PSP6qq9L4G4shY/LV834btV0/zJWq1arUC6e3pRWi7GF1/NnxI/HcejX7/AE1SnuFqCn5croP+lDO8Y
+3DAySnkSooKpExcS/f+yvXCSfFZs+eQ+fPmkUULZpNftu0k8sERCPNK/vVhMthnROLOp8Cd7UNzp82YS75d+B2ZO2fOJFx4ZAzV
nXWtFOERp6mmmrpHWLZCkebli58SdV3TCXzwseMUI5E203hcXDyRShvR0yvD8xedqJc+hKWNI2QvB9DY8gzP2l5ALpejsvY+2P7H
KUfi+SQyHu/te4m2Fx143NqO8so6hETEwNbeiUgaGsHyYCM1LR09PT2Uq7WjD5m5xbBycKccQcdPUR3nkgVISubTuSCnYGKfZn45
m9g7uZPMrGxqu7q5U4yoqg6m5tZ4z8zb2vsQHBoJ7pETsLBxgY6uPsU0NLbA72goUi9m4TVTxCLxPZDpM4iNvTPjD4GTiyesbJ3g
xPLF0Bhzl+4+YHwsmuvM4qCpqQkjY+/B8TuGsKgE9P8JXEjLpNrSLmVTnA9zlo8GhdD5nbtN2LRVlerct990Un14vBSKSeBfRXIy
D6NvgC0qWpPOg1AopDV08/Kn/s9mzmXukAxF5dVYumwVycnJQeHNYhQWlaK0TARRReWHsyfMR3R0DN4ycwNjS7weHkUiX0A5dPca
EmZpOHOOR+1rN0opzoUdAjMzc7waAR62DSDybPyEFk9vX1ImqsTZmN+QmXUFz7pG4MoOhrRBglZmr2fMmnx/xBUVVIexhSPlSEkX
kA81zUBCQiKNRcfxwD4cCK5fMOITk6iPGxABPj8FXczG6ugZobtPjsATsZRDcDWPcvgfi6J2WVU9+oYBB/egD+e5WIRqSTuEhZXw
8uZOaA8Nj6Tc+UVlcOcE4/sfl+OBRErXXFgiQqYgi1y+lE4O2tiSG/kFeDUKqGvvp/nZwlwyjmP7n0RVVSUGmZgnNwChYRFgc3xQ
wfSd1q5hWDtykZubC+mTHpiYWqFbNgSPwyGUo7CoBExpYe/mBwsra1L3qAsFFU8QxxdirdJ6oqOtTbLzboMvuI30K+X4I7sQ9xta
UFcvgSD7OjplY7gsuEa5NLT0UCyqR0ubHB19I3RdsfE8XEhKQfcrQM/IiuKSeKkYZH7qzNTIw4uDjt5B9A+9B9N68eR5P0rEdahq
6IaxpTPyC25CdPcpbOwcIWcovfzCKEdq2u8YYNZr5+YPD84RnIy9CH1zL0TGpeOgvQvFKCsrw/qQF06dy8DTjkGqZ3TsHZzd2HBw
9qR7PN431FRVyKLFS8jm7RrYukMHKjv3Mn1IAWqae7BDUx+K6zbSWqkyb4qaliGUf9Wk9srV66CqrgNlFXWm967FyjVK0NhjghWr
laClY4hN2zSxYdMW7NI1gcKGrTRnl7Ye2bnbCIobtxEy/Ysp/dPyoP3EfPGSpdita4ANGzdjxao1zJqmUb+Z1SGER8XB+IDlJ/tz
hrDkP7/9b/72Tv0FyH3n4Q==
""")

def _enable_windows_ansi():
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        m = ctypes.c_ulong()
        k.GetConsoleMode(h, ctypes.byref(m))
        k.SetConsoleMode(h, m.value | 0x0004)
    except Exception:
        os.system("")

def _q256(r, g, b):
    if abs(r - g) < 10 and abs(g - b) < 10:           # greys
        v = (r + g + b) // 3
        if v < 8: return 16
        if v > 248: return 231
        return 232 + (v - 8) * 24 // 240
    c = lambda x: min(5, (x * 6) // 256)
    return 16 + 36 * c(r) + 6 * c(g) + c(b)

def render(size="auto", color=None):
    if color is None:
        color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    cols = shutil.get_terminal_size((80, 24)).columns
    w, h, data = SMALL if (size == "small" or (size == "auto" and cols < LARGE[0] + 4)) else LARGE
    px = zlib.decompress(base64.b64decode("".join(data.split())))
    mode256 = bool(os.environ.get("IQ_256")) or os.environ.get("TERM") in ("linux", "dumb")
    def fg(p):  return ("38;5;%d" % _q256(*p[:3])) if mode256 else "38;2;%d;%d;%d" % p[:3]
    def bg(p):  return ("48;5;%d" % _q256(*p[:3])) if mode256 else "48;2;%d;%d;%d" % p[:3]
    def at(x, y):
        i = (y * w + x) * 4
        return (px[i], px[i + 1], px[i + 2], px[i + 3])
    lines = []
    for y in range(0, h, 2):
        s = ""
        for x in range(w):
            t, b = at(x, y), at(x, y + 1)
            to, bo = t[3] > 0, b[3] > 0
            if not color:
                s += "█" if to and bo else "▀" if to else "▄" if bo else " "
            elif to and bo:
                s += "\033[%s;%sm▀" % (fg(t), bg(b))
            elif to:
                s += "\033[0;%sm▀" % fg(t)
            elif bo:
                s += "\033[0;%sm▄" % fg(b)
            else:
                s += "\033[0m "
        lines.append("  " + s + ("\033[0m" if color else ""))
    return "\n".join(lines)

def show_logo(size="auto"):
    _enable_windows_ansi()
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("\n" + render(size) + "\n")

if __name__ == "__main__":
    show_logo(sys.argv[1] if len(sys.argv) > 1 else "auto")
