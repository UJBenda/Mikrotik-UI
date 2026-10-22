"""Změny portů (jako Port Manager v UniFi): zapnutí, popis, PoE, PVID,
tagované VLAN, STP edge, PoE hlídač (power-cycle při ztrátě pingu) a zamknutí portu na MAC. Ke každé změně se generuje i návratový skript
z aktuální inventury – plán se aplikuje s automatickým návratem (commit-confirm).

Omezení (záměrná):
- tagované VLAN jen v řádcích bridge VLAN s jediným ID; řádky typu "10,20"
  nebo "100-200" controller neupravuje (změna by zasáhla i ostatní VLAN),
- PVID/VLAN/edge jen u portu v bridgi; PoE jen u portu s PoE.
"""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import re
from typing import Any

from .routeros import q

POE_MODES = ("auto-on", "off", "forced-on")
EDGE_MODES = ("auto", "yes", "no", "no-discover", "yes-discover")
LOOP_MODES = ("default", "on", "off")
_COMMENT = re.compile(r"^[\x20-\x7e]{0,80}$")
_MAC = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")
LOCK = "mtc-lock"
# switche s ACL v čipu (Marvell Prestera – CRS3xx/5xx): zámek portu jde do pravidel switche
_PRESTERA = re.compile(r"98DX|98PX|prestera", re.I)


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
    # novější části (PoE hlídač, zámek, bonding) jen když je inventura má – starý otisk zůstane platný
    extra = {
        "poe": sorted((str(e.get("name")), str(e.get("power-cycle-ping-enabled") or ""),
                       str(e.get("power-cycle-ping-address") or ""), str(e.get("power-cycle-ping-timeout") or ""))
                      for e in _rows(inv, "poe")),
        "learn": sorted((str(b.get("interface")), str(b.get("learn") or "")) for b in _rows(inv, "bridge_ports")
                        if not _true(b.get("dynamic")) and b.get("learn") not in (None, "", "auto")),
        "lock": sorted(str(r.get("comment")) for key in ("switch_rules", "bridge_filters")
                       for r in _rows(inv, key) if str(r.get("comment") or "").startswith(LOCK)),
        "bond": sorted((str(b.get("name")), str(b.get("slaves")), str(b.get("mlag-id") or ""))
                       for b in _rows(inv, "bonds")),
        "mlag": sorted((str(b.get("name")), str(b.get("mlag-peer-port") or ""), str(b.get("mlag-priority") or ""))
                       for b in _rows(inv, "bridges") if b.get("mlag-peer-port") not in (None, "", "none"))
                + ([json.dumps(inv["mlag"], sort_keys=True)] if isinstance(inv.get("mlag"), (dict, list)) and inv["mlag"] else []),
    }
    for k, v in extra.items():
        if v:
            data[k] = v
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
    pe = next((e for e in _rows(inv, "poe") if e.get("name") == port), None)
    watchdog = None
    if pe is not None:
        watchdog = {"enabled": _true(pe.get("power-cycle-ping-enabled")),
                    "address": str(pe.get("power-cycle-ping-address") or ""),
                    "timeout": duration_s(pe.get("power-cycle-ping-timeout")) or 5}
    lock = next((str(h.get("mac-address") or "").upper() for h in _rows(inv, "bridge_hosts")
                 if h.get("on-interface") == port and not _true(h.get("dynamic")) and not _true(h.get("local"))
                 and _lock_rule_exists(inv, port)), None)
    bond = next((str(b.get("name")) for b in _rows(inv, "bonds") if port in _members(b.get("slaves"))), None)
    return {
        "port": port, "disabled": _true(iface.get("disabled")), "comment": iface.get("comment") or "",
        "poe": eth.get("poe-out") if eth.get("poe-out") not in (None, "") else None,
        "loop_protect": eth.get("loop-protect") or None, "loop_status": eth.get("loop-protect-status") or None,
        "bridge": bp.get("bridge") if bp else None, "pvid": int(bp["pvid"]) if bp and str(bp.get("pvid", "")).isdigit() else None,
        "edge": bp.get("edge") if bp else None, "tagged": sorted(tagged, key=int), "tagged_locked": multi,
        "vlan_filtering": _true(br.get("vlan-filtering")),
        "poe_watchdog": watchdog, "learn": (bp.get("learn") or "auto") if bp else None, "lock": lock, "bond": bond,
    }


def _lock_rule_exists(inv: dict, port: str) -> bool:
    tag = f"{LOCK} {port}"
    return any(str(r.get("comment") or "") == tag for key in ("switch_rules", "bridge_filters") for r in _rows(inv, key))


def duration_s(v: Any) -> int | None:
    """RouterOS čas ("5s", "1m30s", "00:01:00", "1h") → sekundy."""
    t = str(v or "").strip()
    if not t:
        return None
    if re.fullmatch(r"\d+", t):
        return int(t)
    m = re.fullmatch(r"(\d+):(\d{2}):(\d{2})", t)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    total, ok = 0, False
    for n, u in re.findall(r"(\d+)([wdhms])", t):
        total += int(n) * {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}[u]
        ok = True
    return total if ok else None


def watchdog_target(v: Any) -> str:
    """Cíl PoE hlídače: IPv4/IPv6 adresa nebo MAC (MAC-ping jen přímo z portu)."""
    t = str(v or "").strip()
    if _MAC.match(t.upper()):
        return t.upper()
    try:
        return str(ipaddress.ip_address(t))
    except ValueError:
        raise PortError("PoE hlídač: adresa musí být IP nebo MAC zařízení na portu") from None


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
    if "poe_watchdog" in desired and desired["poe_watchdog"] is not None:
        w = desired["poe_watchdog"]
        if not isinstance(w, dict):
            raise PortError("PoE hlídač: neplatné nastavení")
        old = cur["poe_watchdog"]
        if old is None:
            raise PortError(f"port {port} nemá PoE (nebo inventura zatím nezná nastavení hlídače)")
        on = bool(w.get("enabled"))
        new = {"enabled": on, "address": old["address"], "timeout": old["timeout"]}
        if on:
            new["address"] = watchdog_target(w.get("address"))
            try:
                # výchozí 5 s RouterOS je na restart AP moc krátké – bez zadání 60 s
                new["timeout"] = int(w.get("timeout") or (old["timeout"] if old["enabled"] else 60))
            except (TypeError, ValueError):
                raise PortError("PoE hlídač: neplatný čas") from None
            if not 10 <= new["timeout"] <= 3600:
                raise PortError("PoE hlídač: čas bez odpovědi 10 s – 1 h")
            if cur["poe"] == "off" and desired.get("poe", cur["poe"]) == "off":
                raise PortError("PoE hlídač potřebuje zapnuté PoE")
        if new != old:
            change("poe_watchdog", old, new)
            apply.append(_watchdog_cmd(qp, new))
            revert.append(_watchdog_cmd(qp, old))
    if "lock" in desired and desired["lock"] is not None:
        _lock_plan(inv, cur, str(desired["lock"]).strip().upper(), change, apply, revert, warn)
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
              "tagged": "tagované VLAN", "loop_protect": "loop protect", "poe_watchdog": "PoE hlídač",
              "lock": "zámek portu"}
    summary = f"{port}: " + ", ".join(f"{labels[c['field']]} {fmt(c['before'])} → {fmt(c['after'])}" for c in changes)
    # návrat v opačném pořadí
    return {"changes": changes, "apply": "\n".join(apply) + "\n", "revert": "\n".join(reversed(revert)) + "\n",
            "warnings": warn, "summary": summary}


def _watchdog_cmd(qp: str, w: dict) -> str:
    if not w["enabled"]:
        return f"/interface ethernet poe set [find name={qp}] power-cycle-ping-enabled=no"
    addr = f" power-cycle-ping-address={q(w['address'])}" if w["address"] else ""
    return (f"/interface ethernet poe set [find name={qp}] power-cycle-ping-enabled=yes{addr} "
            f"power-cycle-ping-timeout={int(w['timeout'])}s")


def _lock_plan(inv: dict, cur: dict, mac: str, change, apply: list, revert: list, warn: list) -> None:
    """Zamknutí portu na jednu MAC: MAC pevně na port (statický host, bez učení) a ostatní MAC na portu
    zahodit – v ACL čipu (CRS3xx/5xx), jinak bridge filtrem (jen provoz přes CPU)."""
    port, qp = cur["port"], q(cur["port"])
    if not cur["bridge"]:
        raise PortError(f"port {port} není v bridgi – zamknout nejde")
    if mac and not _MAC.match(mac):
        raise PortError("zámek portu: neplatná MAC")
    old = cur["lock"] or ""
    if mac == old:
        return
    br, tag = q(cur["bridge"]), q(f"{LOCK} {port}")
    eth = next((e for e in _rows(inv, "ethernet") if e.get("name") == port), {})
    sw = str(eth.get("switch") or "")
    chip = next((str(x.get("type") or "") for x in _rows(inv, "switch") if x.get("name") == sw), "")
    hw = bool(sw) and bool(_PRESTERA.search(chip))
    vid = f" vid={int(cur['pvid'] or 1)}" if cur["vlan_filtering"] else ""

    def lock_cmds(m: str) -> list[str]:
        out = [f"/interface bridge port set [find interface={qp} dynamic=no] learn=no",
               f"/interface bridge host add bridge={br} mac-address={m} interface={qp}{vid} comment={tag}"]
        if hw:
            out += [f"/interface ethernet switch rule add switch={q(sw)} ports={qp} "
                    f"src-mac-address={m}/FF:FF:FF:FF:FF:FF comment={tag}",
                    f"/interface ethernet switch rule add switch={q(sw)} ports={qp} new-dst-ports=\"\" comment={tag}"]
        else:
            out.append(f"/interface bridge filter add chain=forward in-interface={qp} "
                       f"src-mac-address=!{m}/FF:FF:FF:FF:FF:FF action=drop comment={tag}")
        return out

    unlock = [f"/interface ethernet switch rule remove [find comment={tag}]",
              f"/interface bridge filter remove [find comment={tag}]",
              f"/interface bridge host remove [find comment={tag}]",
              f"/interface bridge port set [find interface={qp} dynamic=no] learn={cur['learn'] or 'auto'}"]
    unlock_auto = unlock[:3] + [f"/interface bridge port set [find interface={qp} dynamic=no] learn=auto"]
    change("lock", old or None, mac or None)
    # revert se na konci plánu celý otočí: sem jde v pořadí aplikace
    if old:
        apply.extend(unlock_auto)
        revert.extend(reversed(lock_cmds(old)))
    if mac:
        apply.extend(lock_cmds(mac))
        revert.extend(reversed(unlock if not old else unlock_auto))
    if mac and not hw:
        warn.append(f"{port}: switch nemá ACL v čipu – cizí MAC zahodí bridge filtr, jen když provoz jde přes CPU "
                    "(s hardware offloadem jen zakotvení MAC na port)")


def plan_many(inv: dict, items: dict[str, dict], uplinks: set[str] | None = None) -> dict:
    """Plán pro víc portů jednoho zařízení (hromadná úprava, profil na víc portů).

    Porty se plánují postupně nad kopií inventury, do které se promítají změny tagovaných VLAN –
    jinak by druhý port přepsal členství ve VLAN, které nastavil první."""
    work = copy.deepcopy(inv)
    changes, apply, reverts, warns, summary = [], [], [], [], []
    for port in sorted(items, key=_port_key):
        p = plan(work, port, items[port], uplinks)
        if not p["changes"]:
            continue
        for c in p["changes"]:
            changes.append({"port": port, **c})
            if c["field"] == "tagged":
                _apply_tagged(work, port, c["before"], c["after"])
        apply.append(p["apply"])
        reverts.append(p["revert"])
        warns += [w for w in p["warnings"] if w not in warns]
        summary.append(p["summary"])
    if not changes:
        return {"changes": [], "apply": "", "revert": "", "warnings": [], "summary": "beze změny"}
    head = f"{len({c['port'] for c in changes})} portů: " if len(summary) > 1 else ""
    return {"changes": changes, "apply": "".join(apply), "revert": "".join(reversed(reverts)),
            "warnings": warns, "summary": head + "; ".join(summary)}


def _port_key(name: str) -> tuple:
    m = re.match(r"^(\D*)(\d*)", name)
    return (m.group(1), int(m.group(2) or 0), name) if m else (name, 0, name)


def _apply_tagged(inv: dict, port: str, before: list, after: list) -> None:
    bp = next((b for b in _rows(inv, "bridge_ports") if b.get("interface") == port and not _true(b.get("dynamic"))), {})
    bridge = bp.get("bridge")
    rows = inv.setdefault("bridge_vlans", [])
    for vid in after:
        if vid in before:
            continue
        row = next((r for r in rows if isinstance(r, dict) and r.get("bridge") == bridge
                    and str(r.get("vlan-ids")) == vid and not _true(r.get("dynamic"))), None)
        if row:
            row["tagged"] = _members(row.get("tagged")) + [port]
        else:
            rows.append({"bridge": bridge, "vlan-ids": vid, "tagged": [port], "untagged": [], "comment": "mtc"})
    for vid in before:
        if vid in after:
            continue
        for r in rows:
            if isinstance(r, dict) and r.get("bridge") == bridge and str(r.get("vlan-ids")) == vid and not _true(r.get("dynamic")):
                r["tagged"] = [m for m in _members(r.get("tagged")) if m != port]


def fmt(v: Any) -> str:
    if isinstance(v, dict) and "enabled" in v:
        return f"zap. → {v.get('address') or '?'} po {v.get('timeout')} s" if v["enabled"] else "vyp."
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


def bounce_body(inv: dict, port: str, seconds: int = 5) -> str:
    """Restart linky portu (vypnout, počkat, zapnout) – běží na zařízení celé, i když jde o uplink."""
    iface = next((i for i in _rows(inv, "interfaces") if i.get("name") == port and i.get("type") == "ether"), None)
    if not iface:
        raise PortError(f"port {port} na zařízení není")
    if _true(iface.get("disabled")):
        raise PortError(f"port {port} je vypnutý – zapni ho v editoru portu")
    qp = q(port)
    return (f':log warning ("mtc: restart portu " . {qp})\n'
            f"/interface ethernet disable [find name={qp}]\n"
            f":delay {int(seconds)}s\n"
            f"/interface ethernet enable [find name={qp}]\n")

