"""Změny portů (jako Port Manager v UniFi): zapnutí, popis, PoE, PVID,
tagované VLAN, STP edge. Ke každé změně se generuje i návratový skript
z aktuální inventury – plán se aplikuje s automatickým návratem (commit-confirm).

Omezení (záměrná):
- tagované VLAN jen v řádcích bridge VLAN s jediným ID; řádky typu "10,20"
  nebo "100-200" controller neupravuje (změna by zasáhla i ostatní VLAN),
- PVID/VLAN/edge jen u portu v bridgi; PoE jen u portu s PoE.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .routeros import q

POE_MODES = ("auto-on", "off", "forced-on")
EDGE_MODES = ("auto", "yes", "no", "no-discover", "yes-discover")
LOOP_MODES = ("default", "on", "off")
_COMMENT = re.compile(r"^[\x20-\x7e]{0,80}$")


class PortError(ValueError):
    pass


def _rows(inv: dict, key: str) -> list[dict]:
    v = inv.get(key)
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def _true(v: Any) -> bool:
    return str(v).lower() in ("true", "yes")


def _members(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v if str(x)]
    return [x for x in str(v or "").split(",") if x]


def config_basis(inv: dict) -> str:
    """Otisk konfigurace portů (bez stavů a čítačů) – plán propadne, změní-li se."""
    data = {
        "if": sorted((str(i.get("name")), str(i.get("disabled")), str(i.get("comment") or ""))
                     for i in _rows(inv, "interfaces") if i.get("type") == "ether"),
        "eth": sorted((str(e.get("name")), str(e.get("poe-out") or ""), str(e.get("loop-protect") or ""))
                      for e in _rows(inv, "ethernet")),
        "bp": sorted((str(b.get("interface")), str(b.get("bridge")), str(b.get("pvid")), str(b.get("edge") or ""))
                     for b in _rows(inv, "bridge_ports") if not _true(b.get("dynamic"))),
        "bv": sorted((str(v.get("bridge")), str(v.get("vlan-ids")), ",".join(_members(v.get("tagged"))),
                      ",".join(_members(v.get("untagged")))) for v in _rows(inv, "bridge_vlans")
                     if not _true(v.get("dynamic"))),
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def port_state(inv: dict, port: str) -> dict:
    """Aktuální nastavení portu tak, jak ho edituje UI."""
    iface = next((i for i in _rows(inv, "interfaces") if i.get("name") == port and i.get("type") == "ether"), None)
    if not iface:
        raise PortError(f"port {port} na zařízení není")
    eth = next((e for e in _rows(inv, "ethernet") if e.get("name") == port), {})
    bp = next((b for b in _rows(inv, "bridge_ports") if b.get("interface") == port and not _true(b.get("dynamic"))), None)
    tagged, multi = [], []
    if bp:
        for v in _rows(inv, "bridge_vlans"):
            if v.get("bridge") != bp.get("bridge") or _true(v.get("dynamic")):
                continue
            if port in _members(v.get("tagged")):
                ids = str(v.get("vlan-ids") or "")
                (tagged if ids.isdigit() else multi).append(ids)
    br = next((b for b in _rows(inv, "bridges") if bp and b.get("name") == bp.get("bridge")), {})
    return {
        "port": port, "disabled": _true(iface.get("disabled")), "comment": iface.get("comment") or "",
        "poe": eth.get("poe-out") if eth.get("poe-out") not in (None, "") else None,
        "loop_protect": eth.get("loop-protect") or None, "loop_status": eth.get("loop-protect-status") or None,
        "bridge": bp.get("bridge") if bp else None, "pvid": int(bp["pvid"]) if bp and str(bp.get("pvid", "")).isdigit() else None,
        "edge": bp.get("edge") if bp else None, "tagged": sorted(tagged, key=int), "tagged_locked": multi,
        "vlan_filtering": _true(br.get("vlan-filtering")),
    }


def _vid(v: Any) -> int:
    try:
        n = int(str(v))
    except ValueError:
        raise PortError(f"neplatné VLAN ID {v!r}") from None
    if not 1 <= n <= 4094:
        raise PortError(f"VLAN ID {n} mimo rozsah 1–4094")
    return n


def plan(inv: dict, port: str, desired: dict, uplinks: set[str] | None = None) -> dict:
    """Vrátí {changes, apply, revert, warnings, summary}. Nic nemění."""
    cur = port_state(inv, port)
    qp = q(port)
    changes, apply, revert, warn = [], [], [], []

    def change(field: str, before: Any, after: Any) -> None:
        changes.append({"field": field, "before": before, "after": after})

    if "disabled" in desired and bool(desired["disabled"]) != cur["disabled"]:
        new = bool(desired["disabled"])
        change("disabled", cur["disabled"], new)
        apply.append(f"/interface ethernet set [find name={qp}] disabled={'yes' if new else 'no'}")
        revert.append(f"/interface ethernet set [find name={qp}] disabled={'yes' if cur['disabled'] else 'no'}")
    if "comment" in desired and str(desired["comment"]) != cur["comment"]:
        c = str(desired["comment"])
        if not _COMMENT.match(c):
            raise PortError("popis: max. 80 znaků, jen tisknutelné ASCII")
        change("comment", cur["comment"], c)
        apply.append(f"/interface ethernet set [find name={qp}] comment={q(c)}")
        revert.append(f"/interface ethernet set [find name={qp}] comment={q(cur['comment'])}")
    if "poe" in desired and desired["poe"] != cur["poe"]:
        if cur["poe"] is None:
            raise PortError(f"port {port} nemá PoE")
        if desired["poe"] not in POE_MODES:
            raise PortError(f"PoE: {' | '.join(POE_MODES)}")
        change("poe", cur["poe"], desired["poe"])
        apply.append(f"/interface ethernet set [find name={qp}] poe-out={desired['poe']}")
        revert.append(f"/interface ethernet set [find name={qp}] poe-out={cur['poe']}")
    if desired.get("loop_protect") and desired["loop_protect"] != cur["loop_protect"]:
        if desired["loop_protect"] not in LOOP_MODES:
            raise PortError(f"loop protect: {' | '.join(LOOP_MODES)}")
        if cur["loop_protect"] is None:
            raise PortError("stav loop protect není v inventuře – počkej na další inventuru")
        change("loop_protect", cur["loop_protect"], desired["loop_protect"])
        apply.append(f"/interface ethernet set [find name={qp}] loop-protect={desired['loop_protect']}")
        revert.append(f"/interface ethernet set [find name={qp}] loop-protect={cur['loop_protect']}")
    bridge_fields = [f for f in ("pvid", "tagged", "edge") if f in desired]
    if bridge_fields and not cur["bridge"]:
        raise PortError(f"port {port} není v bridgi – VLAN a STP nelze nastavit")
    find_bp = f"[find interface={qp} dynamic=no]"
    if "pvid" in desired and desired["pvid"] not in (None, "") and _vid(desired["pvid"]) != cur["pvid"]:
        new = _vid(desired["pvid"])
        change("pvid", cur["pvid"], new)
        apply.append(f"/interface bridge port set {find_bp} pvid={new}")
        revert.append(f"/interface bridge port set {find_bp} pvid={cur['pvid'] or 1}")
    if "edge" in desired and desired["edge"] and desired["edge"] != cur["edge"]:
        if desired["edge"] not in EDGE_MODES:
            raise PortError(f"edge: {' | '.join(EDGE_MODES)}")
        change("edge", cur["edge"], desired["edge"])
        apply.append(f"/interface bridge port set {find_bp} edge={desired['edge']}")
        revert.append(f"/interface bridge port set {find_bp} edge={cur['edge'] or 'auto'}")
    if "tagged" in desired:
        want = sorted({str(_vid(v)) for v in desired["tagged"] or []}, key=int)
        have = cur["tagged"]
        br = q(cur["bridge"])
        rows = {str(v.get("vlan-ids")): v for v in _rows(inv, "bridge_vlans")
                if v.get("bridge") == cur["bridge"] and not _true(v.get("dynamic"))}
        # VLAN obsažené v řádcích s více ID nebo rozsahem (na celém bridgi, nejen u tohoto portu)
        multi: dict[str, str] = {}
        for ids in rows:
            if ids.isdigit():
                continue
            for part in ids.split(","):
                if "-" in part:
                    lo, _, hi = part.partition("-")
                    if lo.isdigit() and hi.isdigit():
                        for n in range(int(lo), min(int(hi), 4094) + 1):
                            multi.setdefault(str(n), ids)
                elif part.isdigit():
                    multi.setdefault(part, ids)
        for vid in want:
            if vid in have:
                continue
            if vid in multi:
                raise PortError(f"VLAN {vid} je na zařízení v řádku „{multi[vid]}“ – takové řádky controller neupravuje")
            row = rows.get(vid)
            if row:
                old = _members(row.get("tagged"))
                apply.append(f"/interface bridge vlan set [find bridge={br} vlan-ids={vid} dynamic=no] tagged={q(','.join(old + [port]))}")
                revert.append(f"/interface bridge vlan set [find bridge={br} vlan-ids={vid} dynamic=no] tagged={q(','.join(old))}")
            else:
                apply.append(f'/interface bridge vlan add bridge={br} vlan-ids={vid} tagged={qp} comment="mtc"')
                revert.append(f'/interface bridge vlan remove [find bridge={br} vlan-ids={vid} comment="mtc"]')
        for vid in have:
            if vid in want:
                continue
            old = _members(rows[vid].get("tagged"))
            new = [m for m in old if m != port]
            apply.append(f"/interface bridge vlan set [find bridge={br} vlan-ids={vid} dynamic=no] tagged={q(','.join(new))}")
            revert.append(f"/interface bridge vlan set [find bridge={br} vlan-ids={vid} dynamic=no] tagged={q(','.join(old))}")
        if want != have:
            change("tagged", have, want)
    if not changes:
        return {"changes": [], "apply": "", "revert": "", "warnings": [], "summary": "beze změny"}
    if (cur["pvid"] is not None or "tagged" in desired) and not cur["vlan_filtering"] and \
            any(c["field"] in ("pvid", "tagged") for c in changes):
        warn.append("bridge nemá zapnuté vlan-filtering – PVID a tagování se projeví až po jeho zapnutí")
    if uplinks and port in uplinks:
        warn.append(f"{port} je uplink (cesta ke správě) – při chybě se zařízení odpojí a změna se sama vrátí")
    labels = {"disabled": "vypnutí", "comment": "popis", "poe": "PoE", "pvid": "PVID", "edge": "STP edge",
              "tagged": "tagované VLAN", "loop_protect": "loop protect"}
    summary = f"{port}: " + ", ".join(f"{labels[c['field']]} {fmt(c['before'])} → {fmt(c['after'])}" for c in changes)
    # návrat v opačném pořadí
    return {"changes": changes, "apply": "\n".join(apply) + "\n", "revert": "\n".join(reversed(revert)) + "\n",
            "warnings": warn, "summary": summary}


def fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "ano" if v else "ne"
    if isinstance(v, list):
        return ",".join(v) or "–"
    return str(v) if v not in (None, "") else "–"


def poe_cycle_body(inv: dict, port: str, seconds: int = 5) -> str:
    eth = next((e for e in _rows(inv, "ethernet") if e.get("name") == port), None)
    if not eth or eth.get("poe-out") in (None, "", "off"):
        raise PortError(f"port {port} nemá zapnuté PoE")
    return (f':log warning ("mtc: power-cycle PoE " . {q(port)})\n'
            f"/interface ethernet poe power-cycle [find name={q(port)}] duration={int(seconds)}s\n")
