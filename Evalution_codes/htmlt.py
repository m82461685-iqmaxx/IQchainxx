#!/usr/bin/env python3
"""
Read an HTML file, extract every form control (input, textarea, select,
button) plus its form and label info, and save the result as a JSON file
in the same folder, with the same name (page.html -> page.json).

Usage:
    python html_inputs_to_json.py path/to/page.html
    python html_inputs_to_json.py            (asks for the path)

No third-party packages needed.
"""
import json
import sys
from html.parser import HTMLParser
from pathlib import Path


class InputParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self.fields = []
        self.labels = {}          # for-id -> label text
        self._form_idx = None
        self._current = None      # textarea / select / button being read
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
            self._label_for = a.get("for")
            self._label_text = []

    def handle_data(self, data):
        if self._label_for is not None or self._label_text is not None:
            self._label_text.append(data)
        cur = self._current
        if not cur:
            return
        if cur["tag"] == "select" and cur["options"]:
            cur["options"][-1]["text"] += data
        elif "text" in cur:
            cur["text"] += data

    def handle_endtag(self, tag):
        if tag in ("textarea", "button", "select"):
            if self._current:
                if "text" in self._current:
                    self._current["text"] = self._current["text"].strip()
                if "options" in self._current:
                    for o in self._current["options"]:
                        o["text"] = o["text"].strip()
            self._current = None
        elif tag == "label":
            if self._label_for:
                self.labels[self._label_for] = "".join(self._label_text).strip()
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


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else input("HTML file path: ").strip().strip('"')
    html_path = Path(path).expanduser().resolve()

    if not html_path.is_file():
        sys.exit(f"File not found: {html_path}")

    html = html_path.read_text(encoding="utf-8", errors="replace")

    parser = InputParser()
    parser.feed(html)

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
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {len(parser.fields)} fields -> {out_path}")


if __name__ == "__main__":
    main()
