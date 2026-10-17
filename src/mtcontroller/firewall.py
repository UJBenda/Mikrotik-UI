"""Firewall jako zone-based firewall UniFi (zatím jen čtení).

Zóna = seznam rozhraní (/interface list, např. LAN, WAN, GUEST). Matice zón
ukazuje pro každou dvojici zdroj → cíl (chain forward) a zdroj → router
(chain input), co se stane s NOVÝM spojením: první pravidlo, které pro dvojici
platí bez dalších podmínek, rozhodne (accept → povoleno, drop/reject → blokováno);
pravidla s podmínkou (port, protokol, adresa, čas…) před ním jsou výjimky.
Bez rozhodujícího pravidla platí výchozí accept RouterOS.

Návratový provoz (connection-state=established,related) a zahazování invalid
se do matice nepočítají – ukazují se zvlášť, stejně jako v UniFi.
"""

from __future__ import annotations

from typing import Any

BUILTIN = {"all", "none", "dynamic", "static"}
SKIP_ACTIONS = {"passthrough", "log", "add-src-to-address-list", "add-dst-to-address-list", "fasttrack-connection"}
ALLOW, BLOCK = {"accept"}, {"drop", "reject", "tarpit"}
# podmínky, které z pravidla dělají výjimku (neplatí pro celou dvojici zón)
COND_KEYS = ("protocol", "src-port", "dst-port", "port", "src-address", "src-address-list", "dst-address",
             "dst-address-list", "dst-address-type", "src-address-type", "time", "ipsec-policy", "connection-mark",
             "packet-mark", "routing-mark", "src-mac-address", "layer7-protocol", "content", "icmp-options",
             "tcp-flags", "limit", "connection-limit", "hotspot", "in-bridge-port", "out-bridge-port")


def _rows(inv: dict, key: str) -> list[dict]:
    v = inv.get(key)
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def _true(v: Any) -> bool:
    return str(v).lower() in ("true", "yes")


def zones(inv: dict) -> tuple[list[str], dict[str, list[str]]]:
    names = [str(z.get("name")) for z in _rows(inv, "if_lists")
             if z.get("name") and str(z.get("name")) not in BUILTIN and not _true(z.get("builtin"))]
    members: dict[str, list[str]] = {z: [] for z in names}
    for m in _rows(inv, "if_list_members"):
        if _true(m.get("disabled")) or str(m.get("list")) not in members:
            continue
        members[str(m["list"])].append(str(m.get("interface")))
    # člen může být i další seznam – rozvinout o jednu úroveň
    for z, ifs in members.items():
        extra = [i for x in ifs if x in members for i in members[x]]
        members[z] = sorted(set([i for i in ifs if i not in members] + extra))
    return names, members


def _match(rule: dict, side: str, all_zones: list[str], members: dict[str, list[str]]) -> set[str]:
    """Zóny, na které se pravidlo vztahuje na straně in/out."""
    lst, ifc = rule.get(f"{side}-interface-list"), rule.get(f"{side}-interface")
    if lst:
        neg = str(lst).startswith("!")
        name = str(lst).lstrip("!")
        if name == "all":
            return set() if neg else set(all_zones)
        return set(all_zones) - {name} if neg else ({name} & set(all_zones)) or {name}
    if ifc:
        neg = str(ifc).startswith("!")
        name = str(ifc).lstrip("!")
        hit = {z for z in all_zones if name in members.get(z, [])}
        if neg:
            return set(all_zones) - hit if hit else set(all_zones)
        return hit or {f"if:{name}"}
    return set(all_zones)


def _states(rule: dict) -> set[str]:
    return {s for s in str(rule.get("connection-state") or "").split(",") if s}


def describe(rule: dict) -> str:
    """Čitelný popis podmínek pravidla."""
    parts = []
    for side, label in (("in", "z"), ("out", "do")):
        if rule.get(f"{side}-interface-list"):
            parts.append(f"{label} {rule[f'{side}-interface-list']}")
        elif rule.get(f"{side}-interface"):
            parts.append(f"{label} {rule[f'{side}-interface']}")
    if rule.get("protocol"):
        p = str(rule["protocol"])
        if rule.get("dst-port"):
            p += f"/{rule['dst-port']}"
        parts.append(p)
    elif rule.get("dst-port"):
        parts.append(f"port {rule['dst-port']}")
    for k, label in (("src-address", "zdroj"), ("src-address-list", "zdroj ∈"), ("dst-address", "cíl"),
                     ("dst-address-list", "cíl ∈")):
        if rule.get(k):
            parts.append(f"{label} {rule[k]}")
    if rule.get("connection-state"):
        parts.append(f"stav {rule['connection-state']}")
    if rule.get("connection-nat-state"):
        parts.append(f"NAT {rule['connection-nat-state']}")
    if rule.get("time"):
        parts.append(f"čas {rule['time']}")
    shown = {"protocol", "dst-port", "src-address", "src-address-list", "dst-address", "dst-address-list", "time"}
    for k in COND_KEYS:
        if k not in shown and rule.get(k):
            parts.append(f"{k}={rule[k]}")
    if rule.get("to-addresses") or rule.get("to-ports"):
        parts.append(f"→ {rule.get('to-addresses') or ''}{':' + str(rule['to-ports']) if rule.get('to-ports') else ''}")
    if rule.get("jump-target"):
        parts.append(f"→ {rule['jump-target']}")
    return ", ".join(parts) or "vše"


def _kind(rule: dict) -> str:
    """return / invalid / skip / cond / uncond"""
    if _true(rule.get("disabled")) or _true(rule.get("invalid")):
        return "skip"
    act = str(rule.get("action") or "accept")
    st = _states(rule)
    if act in SKIP_ACTIONS:
        return "skip"
    if st and st <= {"established", "related", "untracked"}:
        return "return"
    if st == {"invalid"}:
        return "invalid"
    if act == "jump" or any(rule.get(k) for k in COND_KEYS):
        return "cond"
    if st and not ({"new"} <= st):
        return "cond"
    return "uncond"


def _cell(rules: list[dict], pairs: list[tuple[set[str], set[str]]], src: str, dst: str | None) -> dict:
    exceptions: list[int] = []
    for r, (ins, outs) in zip(rules, pairs):
        if src not in ins or (dst is not None and dst not in outs):
            continue
        k = r["_kind"]
        if k == "cond":
            exceptions.append(r["index"])
        elif k == "uncond":
            act = str(r.get("action") or "accept")
            if act in ALLOW or act in BLOCK:
                note = None
                if act in BLOCK and r.get("connection-nat-state") == "!dstnat":
                    note = "kromě přesměrovaných portů (dstnat)"
                return {"decision": "allow" if act in ALLOW else "block", "rule": r["index"],
                        "exceptions": exceptions, "note": note}
    return {"decision": "allow", "rule": None, "exceptions": exceptions, "note": "výchozí accept (žádné pravidlo)"}


def analyze(inv: dict) -> dict | None:
    if "fw_filter" not in inv:
        return None
    zl, members = zones(inv)
    rules = []
    for idx, r in enumerate(_rows(inv, "fw_filter")):
        rules.append(dict(r, index=idx, _kind=_kind(r)))
    out: dict[str, Any] = {"zones": zl, "members": members, "matrix": {}, "router": {},
                           "return_traffic": {}, "drop_invalid": {}}
    for chain in ("forward", "input"):
        ch = [r for r in rules if r.get("chain") == chain]
        pairs = [(_match(r, "in", zl, members), _match(r, "out", zl, members)) for r in ch]
        out["return_traffic"][chain] = any(r["_kind"] == "return" and r.get("action") in ("accept", "fasttrack-connection")
                                           for r in ch)
        out["drop_invalid"][chain] = any(r["_kind"] == "invalid" and r.get("action") in BLOCK for r in ch)
        for src in zl:
            if chain == "forward":
                out["matrix"][src] = {dst: _cell(ch, pairs, src, dst) for dst in zl}
            else:
                out["router"][src] = _cell(ch, pairs, src, None)
    out["rules"] = [{"index": r["index"], "chain": r.get("chain"), "action": r.get("action") or "accept",
                     "summary": describe(r), "comment": r.get("comment"), "disabled": _true(r.get("disabled")),
                     "dynamic": _true(r.get("dynamic")), "invalid": _true(r.get("invalid")),
                     "packets": _int(r.get("packets")), "bytes": _int(r.get("bytes")), "log": _true(r.get("log")),
                     "kind": r["_kind"]} for r in rules]
    out["nat"] = [{"index": i, "chain": r.get("chain"), "action": r.get("action"), "summary": describe(r),
                   "comment": r.get("comment"), "disabled": _true(r.get("disabled")), "dynamic": _true(r.get("dynamic")),
                   "packets": _int(r.get("packets")), "bytes": _int(r.get("bytes"))}
                  for i, r in enumerate(_rows(inv, "fw_nat"))]
    notes = []
    if not zl:
        notes.append("Zařízení nemá žádné seznamy rozhraní (zóny) – matice je prázdná; pravidla jsou níže.")
    if zl and not out["return_traffic"]["forward"] and out["matrix"]:
        notes.append("Ve forward chybí povolení návratového provozu (established,related) – odpovědi mohou být blokované.")
    if not out["drop_invalid"]["forward"]:
        notes.append("Ve forward chybí zahazování invalid spojení (doporučení MikroTiku).")
    out["notes"] = notes
    return out


def _int(v: Any) -> int | None:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None
