#!/usr/bin/env python3
"""Překlady rozhraní: výpis zdrojových textů a kontrola slovníků.

Zdrojový jazyk je čeština – český text je zároveň klíč. Texty jsou:
  - v JS: __("text") a __`text ${hodnota}` (klíč má místo hodnot {0}, {1}, …),
  - ve statickém HTML: každý textový uzel a atributy title / placeholder / aria-label / alt.

  tools/i18n.py template          přepíše src/mtcontroller/i18n/template.json (prázdná šablona pro nový jazyk)
  tools/i18n.py check en          vypíše chybějící a přebytečné klíče slovníku
  tools/i18n.py sync en           doplní do slovníku chybějící klíče (prázdné) a přebytečné odstraní
"""

from __future__ import annotations

import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src/mtcontroller/web/static"
I18N = ROOT / "src/mtcontroller/i18n"
JS_FILES = ["app.js"]
HTML_FILES = ["index.html"]
ATTRS = ("title", "placeholder", "aria-label", "alt")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from i18n_wrap import skip_expr, skip_string  # noqa: E402

_ESC = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'", "`": "`", "$": "$", "0": "\0"}


def unescape(s: str) -> str:
    def rep(m: re.Match) -> str:
        c = m.group(1)
        if c[0] == "u":
            return chr(int(c[1:], 16))
        return _ESC.get(c, c)
    return re.sub(r"\\(u[0-9a-fA-F]{4}|.)", rep, s, flags=re.S)


def js_keys(src: str) -> list[str]:
    out = []
    for m in re.finditer(r"(?<![\w$])__([(`])", src):
        i = m.end() - 1
        if src[i] == "(":
            j = i + 1
            while src[j] in " \t\n":
                j += 1
            if src[j] not in "\"'":
                continue
            k = skip_string(src, j)
            out.append(unescape(src[j + 1:k - 1]))
        else:
            j, cur, key, n = i + 1, [], "", 0
            while src[j] != "`":
                if src[j] == "\\":
                    cur.append(src[j:j + 2])
                    j += 2
                elif src[j:j + 2] == "${":
                    j = skip_expr(src, j + 2)
                    cur.append("{%d}" % n)
                    n += 1
                else:
                    cur.append(src[j])
                    j += 1
            out.append(unescape("".join(cur)))
    return out


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


class _Html(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.keys: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        for k, v in attrs:
            if k in ATTRS and v and norm(v):
                self.keys.append(norm(v))

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip and norm(data) and re.search(r"[A-Za-zÀ-ž]", data):
            self.keys.append(norm(data))


def html_keys(src: str) -> list[str]:
    p = _Html()
    p.feed(src)
    return p.keys


def source_keys() -> list[str]:
    keys: list[str] = []
    for f in HTML_FILES:
        keys += html_keys((STATIC / f).read_text(encoding="utf-8"))
    for f in JS_FILES:
        keys += js_keys((STATIC / f).read_text(encoding="utf-8"))
    seen, out = set(), []
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def load(code: str) -> dict:
    return json.loads((I18N / f"{code}.json").read_text(encoding="utf-8"))


def problems(code: str) -> dict:
    """{missing, extra, placeholders} – placeholders = překlad nemá stejné {n} jako klíč."""
    keys = source_keys()
    strings = load(code)["strings"]
    ph = lambda s: sorted(set(re.findall(r"\{\d+\}", s)))  # noqa: E731
    return {"missing": [k for k in keys if not strings.get(k)],
            "extra": [k for k in strings if k not in set(keys)],
            "placeholders": [k for k in keys if strings.get(k) and ph(k) != ph(strings[k])]}


def dump(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=0, sort_keys=False).replace("\n\n", "\n") + "\n", encoding="utf-8")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "template":
        dump(I18N / "template.json", {"language": "", "code": "", "locale": "", "strings": {k: "" for k in source_keys()}})
        print(f"{len(source_keys())} textů → {I18N / 'template.json'}")
    elif cmd in ("check", "sync") and len(sys.argv) > 2:
        code = sys.argv[2]
        pr = problems(code)
        if cmd == "sync":
            d = load(code)
            keys = source_keys()
            d["strings"] = {k: d["strings"].get(k, "") for k in keys}
            dump(I18N / f"{code}.json", d)
        for kind, items in pr.items():
            print(f"{kind}: {len(items)}")
            for k in items[:40]:
                print("  ", json.dumps(k, ensure_ascii=False))
        sys.exit(1 if cmd == "check" and (pr["missing"] or pr["placeholders"]) else 0)
    else:
        print(__doc__)
        sys.exit(2)
