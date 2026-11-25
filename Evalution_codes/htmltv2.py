#!/usr/bin/env python3
"""
html_inputs_to_json.py  -  works on Termux (Android), Linux, Windows, macOS.

Reads an HTML file, extracts every form control (input, textarea, select,
button) plus form and label info, and saves a JSON file in the SAME folder
with the SAME name (page.html -> page.json).

Usage:
    python html_inputs_to_json.py page.html
    python html_inputs_to_json.py page1.html page2.html
    python html_inputs_to_json.py /path/to/folder     (all .html/.htm inside)
    python html_inputs_to_json.py                     (asks for the path)

Termux setup (once):
    pkg install python
    termux-setup-storage          # lets you reach /sdcard and ~/storage
    python html_inputs_to_json.py ~/storage/shared/Download/page.html

Windows examples:
    python html_inputs_to_json.py "C:\\Users\\me\\Desktop\\page.html"

Only the Python standard library is used.
"""
import json
import os
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

HTML_EXTS = {".html", ".htm", ".xhtml"}
IS_TERMUX = "com.termux" in os.environ.get("PREFIX", "") or "TERMUX_VERSION" in os.environ


# ---------------------------------------------------------------- console --
def setup_console():
    """Avoid UnicodeEncodeError on Windows consoles / odd Termux locales."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ------------------------------------------------------------------ paths --
def clean_path(raw: str) -> Path:
    """Handle quotes, ~, %VAR%/$VAR, file:// URLs, and Windows/Unix slashes."""
    raw = raw.strip().strip('"').strip("'")
    if raw.lower().startswith("file:"):
        raw = unquote(urlparse(raw).path)
        # file:///C:/x/y.html  ->  /C:/x/y.html  -> C:/x/y.html on Windows
        if os.name == "nt" and len(raw) > 2 and raw[0] == "/" and raw[2] == ":":
            raw = raw[1:]
    raw = os.path.expandvars(os.path.expanduser(raw))
    return Path(raw)


def collect_files(paths):
    files = []
    for p in paths:
        if p.is_dir():
            files.extend(sorted(f for f in p.iterdir()
                                if f.is_file() and f.suffix.lower() in HTML_EXTS))
        elif p.is_file():
            files.append(p)
        else:
            print(f"[skip] not found: {p}")
            if IS_TERMUX:
                print("       Termux tip: run `termux-setup-storage` once, then use "
                      "~/storage/shared/... or /sdcard/...")
    return files


def read_text(path: Path) -> str:
    """Read HTML with encoding detection (UTF-8, UTF-8 BOM, UTF-16, cp1252)."""
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


# ----------------------------------------------------------------- parser --
class InputParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self.fields = []
        self.labels = {}          # for-id -> label text
        self._form_idx = None
        self._current = None      # textarea / select / button being read
        self._in_label = False
        self._label_for = None
        self._label_text = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v if v is not None else "") for k, v in attrs}

        if tag == "form":
            self.forms.append({k: a.get(k) for k in ("id", "name", "action", "method")})
            self._form_idx = len(self.forms) - 1

        elif tag == "input":
            self._add_field("input", a, a.get("type", "text").lower())

        elif tag in ("textarea", "button"):
            self._current = self._add_field(tag, a, a.get("type", tag), text="")

        elif tag == "select":
            self._current = self._add_field("select", a, "select", options=[])

        elif tag == "option" and self._current and self._current["tag"] == "select":
            self._current["options"].append(
                {"value": a.get("value"), "text": "", "selected": "selected" in a}
            )

        elif tag == "label":
            self._in_label = True
            self._label_for = a.get("for")
            self._label_text = []

    def handle_data(self, data):
        if self._in_label:
            self._label_text.append(data)
        cur = self._current
        if not cur:
            return
        if cur["tag"] == "select":
            if cur["options"]:
                cur["options"][-1]["text"] += data
        elif "text" in cur:
            cur["text"] += data

    def handle_endtag(self, tag):
        if tag in ("textarea", "button", "select"):
            cur = self._current
            if cur:
                if "text" in cur:
                    cur["text"] = cur["text"].strip()
                for o in cur.get("options", []):
                    o["text"] = o["text"].strip()
            self._current = None
        elif tag == "label":
            if self._label_for:
                self.labels[self._label_for] = " ".join("".join(self._label_text).split())
            self._in_label = False
            self._label_for = None
            self._label_text = []
        elif tag == "form":
            self._form_idx = None

    def _add_field(self, tag, attrs, field_type, **extra):
        field = {"tag": tag, "type": field_type, "form_index": self._form_idx}
        field.update(attrs)
        field.update(extra)
        self.fields.append(field)
        return field


# ------------------------------------------------------------------- main --
def process(html_path: Path) -> bool:
    try:
        html = read_text(html_path)
    except OSError as e:
        print(f"[error] cannot read {html_path}: {e}")
        return False

    parser = InputParser()
    parser.feed(html)
    parser.close()

    for field in parser.fields:
        fid = field.get("id")
        if fid and fid in parser.labels:
            field["label"] = parser.labels[fid]

    result = {
        "source_file": html_path.name,
        "total_forms": len(parser.forms),
        "total_fields": len(parser.fields),
        "forms": parser.forms,
        "fields": parser.fields,
    }

    out_path = html_path.with_suffix(".json")   # same folder, same name
    try:
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False),
                            encoding="utf-8")
    except OSError as e:
        print(f"[error] cannot write {out_path}: {e}")
        return False

    print(f"[ok] {len(parser.fields)} fields -> {out_path}")
    return True


def main():
    setup_console()
    args = sys.argv[1:]
    if not args:
        try:
            raw = input("HTML file or folder path: ")
        except (EOFError, KeyboardInterrupt):
            sys.exit(1)
        args = [raw]

    files = collect_files([clean_path(a) for a in args if a.strip()])
    if not files:
        sys.exit("No HTML files found.")

    ok = sum(process(f) for f in files)
    sys.exit(0 if ok == len(files) else 1)


if __name__ == "__main__":
    main()
