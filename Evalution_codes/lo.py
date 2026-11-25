"""
IQchain CLI logo

Prints a big "IQ" block logo, and beside/below it a smaller "chain" word
(~30% the height of IQ) with a chain-link symbol next to it.
Colors used: white, grey, black (as ANSI terminal colors).

Run:
    python iqchain_logo.py            (colored)
    python iqchain_logo.py --no-color (plain, for terminals without ANSI)
"""
import argparse

RESET = "\033[0m"
BOLD = "\033[1m"
WHITE = "\033[97m"
GREY = "\033[37m"
BLACK = "\033[90m"   # bright-black: reads as dark grey/black on most terminals

# ---------------------------------------------------------------- big font (IQ)
# 5 rows tall each letter; '#' = filled pixel, '.' = empty.
# Full, solid letterforms (I as a thick bar, Q as a complete closed loop + tail)
# so they read clearly as whole letters once scaled up, like a brand mark.
BIG_FONT = {
    "I": ["#####", "..#..", "..#..", "..#..", "..#..", "#####"],
    "Q": [".###.", "#...#", "#...#", "#...#", "#..##", ".####"],
}

# ------------------------------------------------------------- small font (chain)
# 3 rows tall each letter -> ~30% the height of the big IQ logo (5*2=10 rows)
SMALL_FONT = {
    "c": ["##", "#.", "##"],
    "h": ["#.#", "###", "#.#"],
    "a": [".#.", "###", "#.#"],
    "i": ["#", "#", "#"],
    "n": ["#.#", "###", "#.#"],
}


def render_word(word, font, scale=1, gap=1):
    """Turn a word into a list of printable text rows using the given bitmap font."""
    letters = [font[ch] for ch in word]
    height = len(letters[0]) * scale
    rows = [""] * height
    for li, letter in enumerate(letters):
        for r, line in enumerate(letter):
            cell = "".join(ch * scale if ch == "#" else " " * scale for ch in line)
            for s in range(scale):
                rows[r * scale + s] += cell
        if li != len(letters) - 1:
            for i in range(height):
                rows[i] += " " * gap
    # '#' -> block char for display
    return [row.replace("#", "\u2588") for row in rows]


def chain_symbol(height=3):
    """A tiny ASCII chain-link graphic, sized to sit next to the 'chain' word."""
    rows = [
        " __    __  ",
        "(__)--(__)=",
        "           ",
    ]
    return rows[:height] if height <= len(rows) else rows


def colorize(rows, color_fn, use_color=True):
    out = []
    n = len(rows)
    for i, row in enumerate(rows):
        if not use_color:
            out.append(row)
            continue
        color = color_fn(i, n)
        out.append(f"{color}{row}{RESET}")
    return out


def iq_gradient(i, n):
    """Bold top -> white, middle -> grey, bottom -> black(ish): a highlighted brand mark."""
    third = max(n // 3, 1)
    if i < third:
        return BOLD + WHITE
    if i < 2 * third:
        return BOLD + GREY
    return BOLD + BLACK


def main():
    ap = argparse.ArgumentParser(description="IQchain CLI logo")
    ap.add_argument("--no-color", action="store_true", help="plain text, no ANSI colors")
    args = ap.parse_args()
    use_color = not args.no_color

    # IQ is the brand: full, solid letters, scaled up big and bold.
    iq_rows = render_word("IQ", BIG_FONT, scale=3, gap=3)           # 18 rows (big, bold)
    # "chain" is the product tag: kept small next to it, like a subtitle/badge.
    chain_rows = render_word("chain", SMALL_FONT, scale=1, gap=1)   # 3 rows (~17% of 18)
    link_rows = chain_symbol(height=len(chain_rows))                # 3 rows

    iq_h = len(iq_rows)
    small_h = len(chain_rows)
    iq_w = max(len(r) for r in iq_rows)
    small_w = max(len(r) for r in chain_rows) + 2 + max(len(r) for r in link_rows)

    # build the small "chain<symbol>" block, bottom-aligned next to IQ
    small_block = []
    for i in range(small_h):
        text = chain_rows[i].ljust(max(len(r) for r in chain_rows))
        link = link_rows[i] if i < len(link_rows) else ""
        if use_color:
            text = f"{GREY}{text}{RESET}"
            link = f"{WHITE}{link}{RESET}"
        small_block.append(text + "  " + link)

    iq_rows_padded = [r.ljust(iq_w) for r in iq_rows]   # pad BEFORE adding color codes
    iq_colored = colorize(iq_rows_padded, iq_gradient, use_color)

    print()
    blank_rows = iq_h - small_h   # chain sits at the bottom rows, beside IQ (not below it)
    for i in range(iq_h):
        left = "  " + iq_colored[i]
        if i >= blank_rows:
            right = "   " + small_block[i - blank_rows]
        else:
            right = ""
        print(left + right)
    print()


if __name__ == "__main__":
    main()
