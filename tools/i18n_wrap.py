#!/usr/bin/env python3
"""Jednorázový pomocník: obalí české texty v JS voláním __() / tagem __`…`.

Projde zdroják tokenizerem (komentáře, řetězce, šablony s vnořenými ${…}, regulární výrazy)
a obalí literály, které vypadají jako text rozhraní. Nové texty už piš rovnou s __().
Použití: i18n_wrap.py soubor.js [--list] [--include soubor_se_slovy]
"""

from __future__ import annotations

import re
import sys

CZ = re.compile(r"[áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]")
CAP = re.compile(r"^[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][a-zá-ž]+(?=$|[ ,.:?!…(–-])")
DENY = {"ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Enter", "Escape", "Tab", "Backspace", "Delete", "Bearer",
        "Content-Type", "MikroTik", "Let's Encrypt", "Infinity", "Switch", "Access point", "Czech Republic"}
REGEX_PREV = set("(,=:[!&|?{};+-*%<>~^") | {""}
SKIP_BEFORE = re.compile(r"(includes|startsWith|endsWith|indexOf|localeCompare|getElementById|querySelector(All)?|"
                         r"addEventListener|setAttribute|getAttribute|createElement(NS)?|svgEl|\$)\(\s*$|"
                         r"(===|!==|==|!=)\s*$|\bcase\s+$|\b__\(\s*$|\btypeof\s+\w+\s*[!=]==?\s*$")


def is_text(s: str, include: set[str]) -> bool:
    if not re.search(r"[A-Za-zá-žÁ-Ž]", s) or s in DENY:
        return False
    if s in include or CZ.search(s):
        return True
    if s.startswith(("/", "#", ".", "http", "M$", "data:")) or re.fullmatch(r"[\w.-]+", s) and not CAP.match(s):
        return False
    if CAP.match(s.lstrip(" („")):
        return True
    return False


def skip_string(src: str, k: int) -> int:
    """k = index úvodní uvozovky; vrací index za koncovou."""
    q = src[k]
    k += 1
    while src[k] != q:
        k += 2 if src[k] == "\\" else 1
    return k + 1


def skip_template(src: str, k: int) -> int:
    """k = index úvodního `; vrací index za koncovým."""
    k += 1
    while src[k] != "`":
        if src[k] == "\\":
            k += 2
        elif src[k:k + 2] == "${":
            k = skip_expr(src, k + 2)
        else:
            k += 1
    return k + 1


def skip_expr(src: str, k: int) -> int:
    """k = index za `${`; vrací index za odpovídající `}`."""
    depth = 1
    prev = "("
    while depth:
        ch = src[k]
        if ch in "\"'":
            k = skip_string(src, k)
            prev = '"'
        elif ch == "`":
            k = skip_template(src, k)
            prev = "`"
        elif ch == "/" and prev in REGEX_PREV:
            k += 1
            in_cls = False
            while src[k] != "/" or in_cls:
                if src[k] == "\\":
                    k += 1
                elif src[k] == "[":
                    in_cls = True
                elif src[k] == "]":
                    in_cls = False
                k += 1
            k += 1
            prev = "/"
        else:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            if not ch.isspace():
                prev = ch
            k += 1
    return k


def transform(src: str, include: set[str], found: list | None = None, skipped: list | None = None) -> str:
    out: list[str] = []
    i, n = 0, len(src)
    prev_sig = ""          # poslední významný znak (kvůli regexům a klíčům objektů)

    def emit(x: str) -> None:
        nonlocal prev_sig
        out.append(x)
        st = x.rstrip()
        if st:
            prev_sig = st[-1]

    def before() -> str:
        return "".join(out[-90:])[-90:]

    while i < n:
        c = src[i]
        two = src[i:i + 2]
        if two == "//":
            j = src.find("\n", i)
            j = n if j < 0 else j
            out.append(src[i:j])
            i = j
        elif two == "/*":
            j = src.find("*/", i) + 2
            out.append(src[i:j])
            i = j
        elif c in "\"'":
            j = i + 1
            while src[j] != c:
                j += 2 if src[j] == "\\" else 1
            lit = src[i:j + 1]
            body = lit[1:-1]
            k = j + 1
            while k < n and src[k] in " \t":
                k += 1
            is_key = k < n and src[k] == ":" and prev_sig in "{,"
            ctx = before()
            if is_text(body, include) and not is_key:
                if SKIP_BEFORE.search(ctx):
                    if skipped is not None:
                        skipped.append((src.count("\n", 0, i) + 1, lit))
                    emit(lit)
                else:
                    if found is not None:
                        found.append(body)
                    emit(f"__({lit})")
            else:
                emit(lit)
            i = j + 1
        elif c == "`":
            # šablona: statické části + rekurzivně zpracované výrazy
            j = i + 1
            parts: list[str] = []      # střídavě text, výraz, text…
            cur = []
            while src[j] != "`":
                if src[j] == "\\":
                    cur.append(src[j:j + 2])
                    j += 2
                elif src[j:j + 2] == "${":
                    k = skip_expr(src, j + 2)
                    parts.append("".join(cur))
                    cur = []
                    parts.append(transform(src[j + 2:k - 1], include, found, skipped))
                    j = k
                else:
                    cur.append(src[j])
                    j += 1
            parts.append("".join(cur))
            static = "".join(parts[0::2])
            body = "".join(p if idx % 2 == 0 else "${" + p + "}" for idx, p in enumerate(parts))
            ctx = before()
            tagged = ctx.rstrip().endswith("__")
            generic = parts[0] + "".join("{}" + parts[idx + 1] for idx in range(1, len(parts), 2))
            if (is_text(static, include) or generic in include) and not tagged and not SKIP_BEFORE.search(ctx):
                if found is not None:
                    key = parts[0] + "".join("{%d}" % (idx // 2) + parts[idx + 1] for idx in range(1, len(parts), 2))
                    found.append(key)
                emit("__`" + body + "`")
            else:
                emit("`" + body + "`")
            i = j + 1
        elif c == "/" and prev_sig in REGEX_PREV or c == "/" and re.search(r"\b(return|typeof)\s*$", before()):
            j = i + 1
            in_cls = False
            while src[j] != "/" or in_cls:
                if src[j] == "\\":
                    j += 1
                elif src[j] == "[":
                    in_cls = True
                elif src[j] == "]":
                    in_cls = False
                elif src[j] == "\n":
                    break
                j += 1
            if src[j] == "\n":           # nebyl to regex (dělení)
                emit(c)
                i += 1
            else:
                j += 1
                while j < n and src[j].isalpha():
                    j += 1
                emit(src[i:j])
                i = j
        else:
            emit(c)
            i += 1
    return "".join(out)


if __name__ == "__main__":
    path = sys.argv[1]
    include: set[str] = set()
    if "--include" in sys.argv:
        include = {ln.rstrip("\n") for ln in open(sys.argv[sys.argv.index("--include") + 1], encoding="utf-8") if ln.strip()}
    src = open(path, encoding="utf-8").read()
    found: list[str] = []
    skipped: list = []
    res = transform(src, include, found, skipped)
    if "--list" in sys.argv:
        for ln, lit in skipped:
            print(f"SKIP {ln}: {lit}")
        print(f"{len(found)} literálů, {len(set(found))} různých, {len(skipped)} přeskočeno (porovnání apod.)")
    else:
        open(path, "w", encoding="utf-8").write(res)
        print(f"obaleno {len(found)} literálů ({len(set(found))} různých)")
