#!/usr/bin/env python3
"""IQchain CLI logo - works on Termux, Linux, macOS and Windows (Terminal/PowerShell/cmd).

Usage:  python iqchain_logo.py
        from iqchain_logo import show_logo; show_logo()
Respects NO_COLOR; falls back to plain ASCII if the terminal can't print Unicode.
"""
import os
import sys

LOGO = [
    "██╗  ██████╗  ██████╗██╗  ██╗ █████╗ ██╗███╗   ██╗",
    "██║ ██╔═══██╗██╔════╝██║  ██║██╔══██╗██║████╗  ██║",
    "██║ ██║   ██║██║     ███████║███████║██║██╔██╗ ██║",
    "██║ ██║▄▄ ██║██║     ██╔══██║██╔══██║██║██║╚██╗██║",
    "██║ ╚██████╔╝╚██████╗██║  ██║██║  ██║██║██║ ╚████║",
    "╚═╝  ╚══▀▀═╝  ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝╚═╝  ╚═══╝",
]

ASCII_LOGO = [
    " ___ ___        _           _        ",
    "|_ _/ _ \\  ___| |__   __ _(_)_ __   ",
    " | | | | |/ __| '_ \\ / _` | | '_ \\  ",
    " | | |_| | (__| | | | (_| | | | | | ",
    "|___\\__\\_\\\\___|_| |_|\\__,_|_|_| |_| ",
]

# chrome / silver gradient (xterm-256 greys), top -> bottom
CHROME = [255, 253, 251, 248, 251, 245]


def _enable_windows_ansi():
    if os.name != "nt":
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        k.GetConsoleMode(h, ctypes.byref(mode))
        k.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        os.system("")  # older trick that also turns on VT mode


def _can_unicode():
    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    return "utf" in enc


def show_logo(tagline="Secure • Smart • Chained"):
    _enable_windows_ansi()
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    art = LOGO if _can_unicode() else ASCII_LOGO
    print()
    for i, line in enumerate(art):
        if color:
            c = CHROME[min(i, len(CHROME) - 1)]
            print(f"  \033[1;38;5;{c}m{line}\033[0m")
        else:
            print("  " + line)
    if tagline:
        print(("  \033[38;5;244m" + tagline + "\033[0m") if color else "  " + tagline)
    print()


if __name__ == "__main__":
    show_logo()
