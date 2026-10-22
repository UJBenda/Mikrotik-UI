"""Agregace portů: LACP (802.3ad) z vybraných portů jednoho switche a MLAG mezi dvěma switchi.

Vše jako běžný plán portů (commit-confirm): když se zařízení po změně neozve, vrátí ji samo.

LACP: porty se z bridge vyjmou, vznikne bonding `bond-mtcN` (802.3ad) a ten se do bridge vloží
s nastavením prvního portu (PVID, edge…); členství ve VLAN se přenese z portů na bond.

MLAG (dva switche, každý se svým plánem): peer port musí být tagovaný ve všech VLAN bridge,
bondy ke klientům mají na obou switchích stejné `mlag-id`. RouterOS 7.22+ nastavuje MLAG
vlastnostmi bridge (`mlag-peer-port`, `mlag-priority`), starší přes `/interface bridge mlag`.
Zdroj: https://help.mikrotik.com/docs/spaces/ROS/pages/67633179/Multi-chassis+Link+Aggregation+Group

Omezení (záměrná, stejná jako u portů): řádky bridge VLAN s víc ID nebo rozsahem controller
neupravuje – port v takovém řádku do bondu nepůjde.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from . import ros_versions
from .portcfg import PortError, _members, _port_key, _rows, _true
from .routeros import q

PREFIX = "bond-mtc"
LACP_RATES = ("30secs", "1sec")
HASHES = ("layer-2", "layer-2-and-3", "layer-3-and-4")
_SAFE = re.compile(r"^[A-Za-z0-9_.:/-]{0,64}$")
MLAG_NEW = "7.22"  # od této verze MLAG ve vlastnostech bridge


def _bp(inv: dict, port: str) -> dict | None:
    return next((b for b in _rows(inv, "bridge_ports") if b.get("interface") == port and not _true(b.get("dynamic"))), None)


def _bond_of(inv: dict, port: str) -> str | None:
    return next((str(b.get("name")) for b in _rows(inv, "bonds") if port in _members(b.get("slaves"))), None)


def free_name(inv: dict) -> str:
    used = {str(i.get("name")) for i in _rows(inv, "interfaces")} | {str(b.get("name")) for b in _rows(inv, "bonds")}
    n = 1
    while f"{PREFIX}{n}" in used:
        n += 1
    return f"{PREFIX}{n}"


def _static_rows(inv: dict, bridge: str) -> list[dict]:
    return [v for v in _rows(inv, "bridge_vlans") if v.get("bridge") == bridge and not _true(v.get("dynamic"))]


def _row_find(bridge: str, vid: str) -> str:
    return f"[find bridge={q(bridge)} vlan-ids={vid} dynamic=no]"


def _set_row(bridge: str, vid: str, tagged: list[str], untagged: list[str]) -> str:
    return (f"/interface bridge vlan set {_row_find(bridge, vid)} tagged={q(','.join(tagged))} "
            f"untagged={q(','.join(untagged))}")


def _port_props(bp: dict) -> str:
    """Vlastnosti bridge portu pro jeho obnovení (jen hodnoty z inventury, které umíme bezpečně zopakovat)."""
    out = []
    for key in ("pvid", "edge", "frame-types", "ingress-filtering", "trusted", "learn"):
        v = str(bp.get(key) or "")
        if v and v not in ("auto",) and _SAFE.match(v):
            out.append(f"{key}={q(v) if not v.isdigit() else v}")
    c = str(bp.get("comment") or "")
    if c and re.fullmatch(r"[\x20-\x7e]{1,80}", c):
        out.append(f"comment={q(c)}")
    return (" " + " ".join(out)) if out else ""


class _Script:
    def __init__(self) -> None:
        self.apply: list[str] = []
        self.revert: list[str] = []  # v pořadí provedení (už otočené)


def _lacp_into(work: dict, sc: _Script, ports: list[str], *, name: str, mlag_id: int | None,
               lacp_rate: str, hash_policy: str, warn: list[str]) -> dict:
    """Přidá do skriptu vytvoření bondu a promítne ho do pracovní kopie inventury."""
    if len(set(ports)) != len(ports) or not ports:
        raise PortError("vyber porty bez opakování")
    if mlag_id is None and len(ports) < 2:
        raise PortError("LACP potřebuje aspoň dva porty")
    if lacp_rate not in LACP_RATES or hash_policy not in HASHES:
        raise PortError("neplatné parametry LACP")
    ifaces = {str(i.get("name")): i for i in _rows(work, "interfaces")}
    bridge = None
    bps = []
    for p in ports:
        i = ifaces.get(p)
        if not i or i.get("type") != "ether":
            raise PortError(f"port {p} na zařízení není (jen fyzické porty)")
        if _bond_of(work, p):
            raise PortError(f"port {p} už je v bondu {_bond_of(work, p)}")
        bp = _bp(work, p)
        if not bp:
            raise PortError(f"port {p} není v bridgi")
        if bridge is None:
            bridge = str(bp.get("bridge"))
        elif bp.get("bridge") != bridge:
            raise PortError("porty musí být ve stejném bridgi")
        bps.append(bp)
    if len({str(b.get("pvid") or "1") for b in bps}) > 1:
        warn.append(f"porty mají různé PVID – bond převezme PVID {bps[0].get('pvid') or 1} z {ports[0]}")
    rows = []
    for v in _static_rows(work, bridge):
        tg, ut = _members(v.get("tagged")), _members(v.get("untagged"))
        if not any(p in tg or p in ut for p in ports):
            continue
        vid = str(v.get("vlan-ids") or "")
        if not vid.isdigit():
            raise PortError(f"port je ve VLAN řádku „{vid}“ – takové řádky controller neupravuje")
        rows.append((v, vid, tg, ut))
    slaves = ",".join(ports)
    mid = f" mlag-id={int(mlag_id)}" if mlag_id is not None else ""
    first = bps[0]
    # 1) členství ve VLAN bez portů, 2) porty z bridge pryč, 3) bond do bridge, 4) VLAN s bondem
    for v, vid, tg, ut in rows:
        sc.apply.append(_set_row(bridge, vid, [m for m in tg if m not in ports], [m for m in ut if m not in ports]))
    for p in ports:
        sc.apply.append(f"/interface bridge port remove [find interface={q(p)} dynamic=no]")
    sc.apply.append(f"/interface bonding add name={q(name)} slaves={q(slaves)} mode=802.3ad lacp-rate={lacp_rate} "
                    f"transmit-hash-policy={hash_policy}{mid} comment=\"mtc\"")
    sc.apply.append(f"/interface bridge port add bridge={q(bridge)} interface={q(name)}"
                    f"{_port_props({k: first.get(k) for k in ('pvid', 'edge', 'frame-types', 'ingress-filtering')})} "
                    "comment=\"mtc\"")
    new_rows = []
    for v, vid, tg, ut in rows:
        ntg = [m for m in tg if m not in ports] + ([name] if any(p in tg for p in ports) else [])
        nut = [m for m in ut if m not in ports] + ([name] if any(p in ut for p in ports) else [])
        sc.apply.append(_set_row(bridge, vid, ntg, nut))
        new_rows.append((v, vid, tg, ut, ntg, nut))
    # návrat v pořadí provedení
    rv = []
    for v, vid, tg, ut, ntg, nut in new_rows:
        rv.append(_set_row(bridge, vid, [m for m in tg if m not in ports], [m for m in ut if m not in ports]))
    rv.append(f"/interface bridge port remove [find interface={q(name)}]")
    rv.append(f"/interface bonding remove [find name={q(name)} comment=\"mtc\"]")
    for p, bp in zip(ports, bps):
        rv.append(f"/interface bridge port add bridge={q(bridge)} interface={q(p)}{_port_props(bp)}")
    for v, vid, tg, ut, ntg, nut in new_rows:
        rv.append(_set_row(bridge, vid, tg, ut))
    sc.revert = rv + sc.revert
    # pracovní kopie: bond místo portů
    for v, vid, tg, ut, ntg, nut in new_rows:
        v["tagged"], v["untagged"] = ntg, nut
    work["bridge_ports"] = [b for b in _rows(work, "bridge_ports") if b.get("interface") not in ports] + [
        {"bridge": bridge, "interface": name, "pvid": first.get("pvid") or "1", "edge": first.get("edge")}]
    work.setdefault("bonds", []).append({"name": name, "slaves": slaves, "mlag-id": mlag_id})
    work.setdefault("interfaces", []).append({"name": name, "type": "bond"})
    return {"bridge": bridge, "name": name}


def lacp_plan(inv: dict, ports: list[str], *, name: str | None = None, lacp_rate: str = "30secs",
              hash_policy: str = "layer-2-and-3", uplinks: set[str] | None = None) -> dict:
    """LACP bond z vybraných portů jednoho switche."""
    ports = sorted({str(p) for p in ports}, key=_port_key)
    work, sc, warn = copy.deepcopy(inv), _Script(), []
    name = name or free_name(inv)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
        raise PortError("neplatné jméno bondu")
    res = _lacp_into(work, sc, ports, name=name, mlag_id=None, lacp_rate=lacp_rate, hash_policy=hash_policy, warn=warn)
    if uplinks and set(ports) & uplinks:
        warn.append("v bondu je uplink (cesta ke správě) – protistrana musí mít LACP taky, jinak se zařízení "
                    "odpojí a změna se za 5 min sama vrátí")
    warn.append("protistrana (switch, server) musí mít na těchto portech LACP (802.3ad)")
    summary = f"LACP {name}: {', '.join(ports)} (bridge {res['bridge']})"
    return _result(sc, warn, summary, [{"field": "bond", "before": None, "after": {"name": name, "ports": ports}}])


def _result(sc: _Script, warn: list[str], summary: str, changes: list[dict]) -> dict:
    return {"changes": changes, "apply": "\n".join(sc.apply) + "\n", "revert": "\n".join(sc.revert) + "\n",
            "warnings": warn, "summary": summary}


# --- MLAG ------------------------------------------------------------------------------------


def _bridge_of(inv: dict, port: str) -> dict:
    bp = _bp(inv, port)
    if not bp:
        raise PortError(f"peer port {port} není v bridgi")
    br = next((b for b in _rows(inv, "bridges") if b.get("name") == bp.get("bridge")), None)
    if not br:
        raise PortError("bridge peer portu chybí v inventuře")
    return br


def _mlag_state(inv: dict, new_syntax: bool) -> dict:
    if new_syntax:
        return {}
    m = inv.get("mlag")
    m = m[0] if isinstance(m, list) and m else m
    return m if isinstance(m, dict) else {}


def mlag_plan(inv: dict, version: str, peer_port: str, members: list[dict], *, primary: bool) -> dict:
    """Plán MLAG pro jeden switch dvojice. members: [{ports: [...], mlag_id: N}]."""
    ver = ros_versions.parse(str(version or "").split(" ")[0])
    if ver < ros_versions.parse("7.10"):
        raise PortError(f"MLAG potřebuje RouterOS 7.10 nebo novější (zařízení má {version or '?'})")
    new_syntax = ver >= ros_versions.parse(MLAG_NEW)
    work, sc, warn = copy.deepcopy(inv), _Script(), []
    peer_port = str(peer_port)
    if not any(i.get("name") == peer_port for i in _rows(inv, "interfaces")):
        raise PortError(f"peer port {peer_port} na zařízení není")
    br = _bridge_of(inv, peer_port)
    bridge = str(br.get("name"))
    if not _true(br.get("vlan-filtering")):
        raise PortError(f"bridge {bridge} nemá zapnuté vlan-filtering – MLAG ho vyžaduje")
    if any(_true(s.get("l3-hw-offloading")) for s in _rows(inv, "switch")):
        raise PortError("na switchi je zapnutý L3 hardware offloading – s MLAG nejde (vypni l3-hw-offloading)")
    if not members:
        raise PortError("vyber aspoň jeden MLAG bond (porty ke klientovi a mlag-id)")
    ids = [int(m.get("mlag_id") or 0) for m in members]
    if any(not 1 <= i <= 4294967295 for i in ids) or len(set(ids)) != len(ids):
        raise PortError("mlag-id musí být kladná a různá čísla")
    member_ports = [str(p) for m in members for p in m.get("ports") or []]
    if peer_port in member_ports:
        raise PortError("peer port nemůže být zároveň v MLAG bondu")
    # 1) peer port tagovaný ve všech VLAN bridge (statické řádky + PVID portů bez řádku)
    used = {str(b.get("pvid") or "1") for b in _rows(inv, "bridge_ports")
            if b.get("bridge") == bridge and not _true(b.get("dynamic")) and b.get("interface") != peer_port}
    rows = _static_rows(work, bridge)
    for v in rows:
        vid = str(v.get("vlan-ids") or "")
        tg, ut = _members(v.get("tagged")), _members(v.get("untagged"))
        if not vid.isdigit():
            if peer_port not in tg:
                warn.append(f"VLAN řádek „{vid}“ má víc ID – peer port do něj přidej ručně")
            continue
        used.discard(vid)
        if peer_port in tg:
            continue
        if peer_port in ut:
            raise PortError(f"peer port je ve VLAN {vid} netagovaný – MLAG potřebuje peer port jen tagovaný")
        sc.apply.append(_set_row(bridge, vid, tg + [peer_port], ut))
        sc.revert.insert(0, _set_row(bridge, vid, tg, ut))
        v["tagged"] = tg + [peer_port]
    for vid in sorted(used, key=int):
        if vid == "1":
            continue  # výchozí VLAN 1 – peer port ji má jako PVID (netagovaně) a tak to nechat
        sc.apply.append(f"/interface bridge vlan add bridge={q(bridge)} vlan-ids={vid} tagged={q(peer_port)} comment=\"mtc\"")
        sc.revert.insert(0, f"/interface bridge vlan remove [find bridge={q(bridge)} vlan-ids={vid} comment=\"mtc\"]")
        rows.append({"bridge": bridge, "vlan-ids": vid, "tagged": [peer_port], "untagged": []})
    # 2) MLAG na bridgi
    prio = 50 if primary else 128
    if new_syntax:
        old_peer = str(br.get("mlag-peer-port") or "none")
        old_prio = str(br.get("mlag-priority") or "128")
        sc.apply.append(f"/interface bridge set [find name={q(bridge)}] mlag-peer-port={q(peer_port)} mlag-priority={prio}")
        sc.revert.insert(0, f"/interface bridge set [find name={q(bridge)}] mlag-peer-port={q(old_peer)} "
                            f"mlag-priority={int(old_prio) if old_prio.isdigit() else 128}")
    else:
        m = _mlag_state(inv, False)
        sc.apply.append(f"/interface bridge mlag set bridge={q(bridge)} peer-port={q(peer_port)} priority={prio}")
        sc.revert.insert(0, f"/interface bridge mlag set bridge={q(str(m.get('bridge') or 'none'))} "
                            f"peer-port={q(str(m.get('peer-port') or 'none'))}")
    # 3) bondy ke klientům se stejným mlag-id na obou switchích
    names = []
    for mem, mid in zip(members, ids):
        ports = sorted({str(p) for p in mem.get("ports") or []}, key=_port_key)
        name = f"{PREFIX}-mlag{mid}"
        if any(b.get("name") == name for b in _rows(work, "bonds")):
            raise PortError(f"bond {name} už existuje")
        res = _lacp_into(work, sc, ports, name=name, mlag_id=mid, lacp_rate="1sec",
                         hash_policy="layer-2-and-3", warn=warn)
        if res["bridge"] != bridge:
            raise PortError("porty MLAG bondu musí být ve stejném bridgi jako peer port")
        names.append(f"{name} ({', '.join(ports)}, mlag-id {mid})")
    warn.append("druhý switch dvojice potřebuje stejný plán (stejná mlag-id, stejná priorita bridge) – "
                "aplikuj oba; klient musí mít LACP")
    if str(br.get("protocol-mode") or "none") == "none":
        warn.append("bridge nemá STP – u MLAG doporučeno RSTP/MSTP se stejnou prioritou na obou switchích")
    role = "primární" if primary else "sekundární"
    summary = f"MLAG ({role}): peer {peer_port}, " + "; ".join(names)
    return _result(sc, warn, summary, [{"field": "mlag", "before": None,
                                        "after": {"peer_port": peer_port, "members": [{"ports": m.get("ports"), "mlag_id": i}
                                                                                      for m, i in zip(members, ids)]}}])


def pair_checks(inv_a: dict, peer_a: str, inv_b: dict, peer_b: str, ident_a: str, ident_b: str) -> list[str]:
    """Kontroly dvojice: peer porty spojené spolu (sousedé), stejná priorita bridge."""
    out = []

    def sees(inv: dict, port: str, other: str) -> bool:
        return any(n.get("identity") == other and str(n.get("interface") or "").split(",")[0] == port
                   for n in _rows(inv, "neighbors"))
    if not (sees(inv_a, peer_a, ident_b) or sees(inv_b, peer_b, ident_a)):
        out.append(f"sousedé nehlásí přímé spojení {ident_a} {peer_a} ↔ {ident_b} {peer_b} – peer porty musí být propojené napřímo")
    try:
        pa, pb = _bridge_of(inv_a, peer_a).get("priority"), _bridge_of(inv_b, peer_b).get("priority")
        if pa and pb and str(pa) != str(pb):
            out.append(f"priorita bridge se liší ({pa} vs {pb}) – u MLAG má být na obou stejná")
    except PortError:
        pass
    return out


def summarize_bonds(inv: dict) -> list[dict]:
    """Bondy zařízení pro UI."""
    return [{"name": str(b.get("name")), "slaves": _members(b.get("slaves")), "mode": b.get("mode"),
             "mlag_id": b.get("mlag-id"), "running": _true(b.get("running")), "mtc": b.get("comment") == "mtc"}
            for b in _rows(inv, "bonds")]


def remove_plan(inv: dict, name: str) -> dict:
    """Zrušení bondu controlleru: porty zpět do bridge s nastavením bondu, VLAN zpět na porty."""
    b = next((x for x in _rows(inv, "bonds") if x.get("name") == name), None)
    if not b or b.get("comment") != "mtc":
        raise PortError("rušit jde jen bond vytvořený controllerem")
    slaves = _members(b.get("slaves"))
    bp = _bp(inv, name)
    if not bp:
        raise PortError(f"bond {name} není v bridgi")
    bridge = str(bp.get("bridge"))
    sc, warn = _Script(), []
    rows = []
    for v in _static_rows(inv, bridge):
        tg, ut = _members(v.get("tagged")), _members(v.get("untagged"))
        if name not in tg and name not in ut:
            continue
        vid = str(v.get("vlan-ids") or "")
        if not vid.isdigit():
            raise PortError(f"bond je ve VLAN řádku „{vid}“ – uprav ručně")
        rows.append((vid, tg, ut))
    for vid, tg, ut in rows:
        sc.apply.append(_set_row(bridge, vid, [m for m in tg if m != name], [m for m in ut if m != name]))
    sc.apply.append(f"/interface bridge port remove [find interface={q(name)}]")
    sc.apply.append(f"/interface bonding remove [find name={q(name)} comment=\"mtc\"]")
    for p in slaves:
        sc.apply.append(f"/interface bridge port add bridge={q(bridge)} interface={q(p)}{_port_props(bp)}")
    for vid, tg, ut in rows:
        sc.apply.append(_set_row(bridge, vid, [m for m in tg if m != name] + (slaves if name in tg else []),
                                 [m for m in ut if m != name] + (slaves if name in ut else [])))
    mid = f" mlag-id={int(b['mlag-id'])}" if str(b.get("mlag-id") or "").isdigit() else ""
    rate = str(b.get("lacp-rate") or "30secs")
    hp = str(b.get("transmit-hash-policy") or "layer-2-and-3")
    sc.revert = [
        *[_set_row(bridge, vid, [m for m in tg if m != name], [m for m in ut if m != name]) for vid, tg, ut in rows],
        *[f"/interface bridge port remove [find interface={q(p)} dynamic=no]" for p in slaves],
        f"/interface bonding add name={q(name)} slaves={q(','.join(slaves))} mode=802.3ad "
        f"lacp-rate={rate if rate in LACP_RATES else '30secs'} transmit-hash-policy={hp if hp in HASHES else 'layer-2-and-3'}"
        f"{mid} comment=\"mtc\"",
        f"/interface bridge port add bridge={q(bridge)} interface={q(name)}{_port_props(bp)}",
        *[_set_row(bridge, vid, tg, ut) for vid, tg, ut in rows],
    ]
    warn.append("protistrana má na portech LACP – po zrušení bondu ho vypni i tam, jinak hrozí smyčka/výpadek")
    return _result(sc, warn, f"zrušení bondu {name} ({', '.join(slaves)})",
                   [{"field": "bond", "before": {"name": name, "ports": slaves}, "after": None}])
