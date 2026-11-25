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

import ipaddress
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


# --- kam pravidlo míří: zóny podle seznamu rozhraní, rozhraní, adresy i address-listu ---------------------------
# Úroveň shody pravidla se zónou: FULL = pravidlo pokrývá celou zónu, PART = jen její část (jedno rozhraní z více,
# jedna adresa z podsítě), 0 = zóny se netýká. Podmínky na jedné straně se kombinují jako průnik (minimum).
FULL, PART = 2, 1
SIDE_KEYS = {"in": ("in-interface-list", "in-interface", "src-address", "src-address-list"),
             "out": ("out-interface-list", "out-interface", "dst-address", "dst-address-list")}
LOCATION_KEYS = {k for ks in SIDE_KEYS.values() for k in ks}


def _nets(value: Any) -> list:
    """Adresa pravidla (IP, CIDR, rozsah a–b) → seznam sítí; IPv6 a jména se přeskočí."""
    out = []
    for part in str(value or "").split(","):
        part = part.strip()
        try:
            if "-" in part:
                a, b = part.split("-", 1)
                out.extend(ipaddress.summarize_address_range(ipaddress.IPv4Address(a.strip()), ipaddress.IPv4Address(b.strip())))
            elif part:
                out.append(ipaddress.IPv4Network(part, strict=False))
        except (ValueError, TypeError):
            continue
    return out


class Ctx:
    """Co je o zónách potřeba vědět k zařazení pravidla: členové, podsítě, rozhraní k internetu, address-listy."""

    def __init__(self, inv: dict, zl: list[str], members: dict[str, list[str]]):
        self.zones, self.members = zl, members
        by_if: dict[str, list] = {}
        for a in _rows(inv, "addresses"):
            if _true(a.get("disabled")) or not a.get("interface"):
                continue
            by_if.setdefault(str(a["interface"]), []).extend(_nets(a.get("address")))
        self.znets = {z: [n for i in members.get(z, []) for n in by_if.get(i, [])] for z in zl}
        wan_if = set()
        for r in _rows(inv, "routes_default"):
            for key in ("immediate-gw", "gateway"):
                v = str(r.get(key) or "")
                if "%" in v:
                    wan_if.add(v.split("%", 1)[1])
        for d in _rows(inv, "dhcp_client"):
            if d.get("interface") and not _true(d.get("disabled")):
                wan_if.add(str(d["interface"]))
        self.wan = {z for z in zl if wan_if & set(members.get(z, []))}
        self.lists: dict[str, list] | None = None
        if "fw_addrlists" in inv:
            self.lists = {}
            for e in _rows(inv, "fw_addrlists"):
                if not _true(e.get("disabled")) and e.get("list"):
                    self.lists.setdefault(str(e["list"]), []).extend(_nets(e.get("address")))

    def by_ifaces(self, ifs: set[str], named: str | None = None) -> dict[str, int]:
        out = {}
        for z in self.zones:
            m = set(self.members.get(z, []))
            if z == named or (m and m <= ifs):
                out[z] = FULL
            elif m & ifs:
                out[z] = PART
        return out

    def by_nets(self, nets: list) -> dict[str, int]:
        out, local = {}, False
        for z in self.zones:
            zn = self.znets.get(z) or []
            if zn and all(any(n.subnet_of(r) for r in nets) for n in zn):
                out[z] = FULL
            elif any(n.overlaps(r) for n in zn for r in nets):
                out[z] = PART
            local = local or z in out
        if not local:
            # adresa mimo všechny místní podsítě = někde za linkou k internetu
            out = {z: PART for z in self.wan}
        return out


def _invert(hit: dict[str, int], zl: list[str]) -> dict[str, int]:
    """Negace (!): co pravidlo pokrývalo celé, vypadne; co částečně, zůstává částečně; ostatní zóny celé."""
    return {z: (PART if hit.get(z) == PART else FULL) for z in zl if hit.get(z) != FULL}


def _side(rule: dict, side: str, ctx: Ctx, addresses: bool = True) -> tuple[dict[str, int], bool]:
    """({zóna: úroveň shody}, je_pravidlo_na_této_straně_omezené_místem)."""
    lst_k, if_k, addr_k, al_k = SIDE_KEYS[side]
    cur = {z: FULL for z in ctx.zones}
    scoped = False
    for key in (lst_k, if_k, addr_k, al_k) if addresses else (lst_k, if_k):
        raw = rule.get(key)
        if not raw:
            continue
        neg, val = str(raw).startswith("!"), str(raw).lstrip("!")
        if key == lst_k:
            if val == "all":
                hit = {z: FULL for z in ctx.zones}
            else:
                hit = ctx.by_ifaces(set(ctx.members.get(val, [])), val if val in ctx.members else None)
        elif key == if_k:
            hit = ctx.by_ifaces({val})
        elif key == addr_k:
            hit = ctx.by_nets(_nets(val))
        else:
            if ctx.lists is None or val not in ctx.lists:
                # seznam adres neznáme (ještě není v inventuře, dynamický, jména) – může se týkat kterékoli zóny
                hit, neg = {z: PART for z in ctx.zones}, False
            else:
                hit = ctx.by_nets(ctx.lists[val])
        if neg:
            hit = _invert(hit, ctx.zones)
        scoped = True
        cur = {z: min(lv, hit[z]) for z, lv in cur.items() if z in hit}
    return cur, scoped


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
    if act == "jump" or any(rule.get(k) for k in COND_KEYS if k not in LOCATION_KEYS or rule.get("chain") == "input" and k.startswith("dst-")):
        return "cond"
    if st and not ({"new"} <= st):
        return "cond"
    return "uncond"


def _cell(rules: list[dict], sides: list[tuple], src: str, dst: str | None) -> dict:
    """Co se stane s novým spojením src → dst: první pravidlo, které dvojici pokrývá celou a bez dalších podmínek,
    rozhodne. Před ním jsou výjimky – jen pravidla, která se místem (rozhraním, adresou) vážou právě k těmto zónám;
    pravidla bez určení místa (např. „povol ICMP odkudkoli“) platí všude a vedou se zvlášť jako obecná."""
    exceptions: list[int] = []
    for r, (ins, in_sc, outs, out_sc) in zip(rules, sides):
        lv = min(ins.get(src, 0), outs.get(dst, 0) if dst is not None else FULL)
        if not lv:
            continue
        k = r["_kind"]
        if k == "cond" or (k == "uncond" and lv == PART):
            if in_sc or out_sc:
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
    ctx = Ctx(inv, zl, members)
    out: dict[str, Any] = {"zones": zl, "members": members, "matrix": {}, "router": {}, "general": {},
                           "return_traffic": {}, "drop_invalid": {},
                           "subnets": {z: sorted({str(n) for n in ctx.znets.get(z, [])}) for z in zl}}
    scope: dict[int, dict] = {}
    for chain in ("forward", "input"):
        ch = [r for r in rules if r.get("chain") == chain]
        pairs = []
        for r in ch:
            ins, in_sc = _side(r, "in", ctx)
            # do routeru (input) cílová adresa neurčuje zónu – je to podmínka
            outs, out_sc = _side(r, "out", ctx, addresses=chain == "forward") if chain == "forward" else ({z: FULL for z in zl}, False)
            pairs.append((ins, in_sc, outs, out_sc))
            scope[r["index"]] = {"src": sorted(ins) if in_sc else None, "dst": sorted(outs) if out_sc else None}
        # obecná pravidla: mají podmínku, ale žádné určení místa – platí pro všechny dvojice zón
        out["general"][chain] = [r["index"] for r, (_i, in_sc, _o, out_sc) in zip(ch, pairs)
                                 if r["_kind"] == "cond" and not in_sc and not out_sc]
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
                     "kind": r["_kind"], "src_zones": scope.get(r["index"], {}).get("src"),
                     "dst_zones": scope.get(r["index"], {}).get("dst")} for r in rules]
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
