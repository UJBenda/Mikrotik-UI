"""Infrastruktura lokality (jako „Infrastructure Topology“ v UniFi Network 10):
strom WAN → gateway → switche → AP s porty na spojích, zátěží, PoE a STP.

Zdroj: inventury spravovaných zařízení – sousedé (MNDP/LLDP) pro spoje,
/ip/route 0.0.0.0/0 a /ip/dhcp-client pro WAN, /interface/bridge/monitor pro
STP root, /system/resource pro CPU a paměť, ethernet/PoE monitor pro porty.
"""

from __future__ import annotations

import re
from collections import deque
from typing import Any

from . import stp

PORT_RE = re.compile(r"^(ether|sfp|combo|qsfp|wlan|wifi)")
PHYS_RE = re.compile(r"^(ether|sfp|combo|qsfp)")


def _rows(inv: dict, key: str) -> list[dict]:
    v = inv.get(key)
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def _obj(inv: dict, key: str) -> dict:
    v = inv.get(key)
    return v if isinstance(v, dict) else {}


def _true(v: Any) -> bool:
    return str(v).lower() in ("true", "yes")


def _port_of(iface: Any) -> str:
    """'bridge/ether3', 'ether3,bridge' → 'ether3' (fyzický port má přednost)."""
    parts = [p for p in re.split(r"[,/]", str(iface or "")) if p]
    for p in parts:
        if PORT_RE.match(p):
            return p
    return parts[0] if parts else ""


def _port_num(name: str) -> str:
    m = re.search(r"(\d+)(?!.*\d)", name or "")
    return m.group(1) if m else (name or "")


def _pct(free: Any, total: Any) -> int | None:
    try:
        return round(100 - 100 * int(free) / int(total))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _ports(inv: dict) -> list[dict]:
    mon = {str(m.get("name")): m for m in _rows(inv, "ethernet_monitor")}
    out = []
    for i in _rows(inv, "interfaces"):
        name = str(i.get("name") or "")
        if i.get("type") != "ether" or not PHYS_RE.match(name):
            continue
        m = mon.get(name, {})
        if _true(i.get("disabled")):
            cls = "off"
        elif not _true(i.get("running")) or (m.get("status") and m.get("status") != "link-ok"):
            cls = "down"
        elif m.get("rate") and "G" not in str(m.get("rate")):
            cls = "fe"
        else:
            cls = "gbe"
        out.append({"name": name, "cls": cls})
    out.sort(key=lambda p: (not p["name"].startswith(("ether", "combo")), int(_port_num(p["name"]) or 0)))
    return out


def _wans(inv: dict) -> list[dict]:
    addrs: dict[str, str] = {}
    for a in _rows(inv, "addresses"):
        if not _true(a.get("disabled")) and a.get("interface"):
            addrs.setdefault(str(a["interface"]), str(a.get("address") or "").split("/")[0])
    dhcp = {str(d.get("interface")): d for d in _rows(inv, "dhcp_client") if not _true(d.get("disabled"))}
    out, seen = [], set()
    routes = [r for r in _rows(inv, "routes_default")
              if not _true(r.get("disabled")) and str(r.get("routing-table") or "main") == "main"]
    for r in sorted(routes, key=lambda r: int(str(r.get("distance") or "1")) if str(r.get("distance") or "1").isdigit() else 99):
        imm = str(r.get("immediate-gw") or "")
        iface = imm.split("%", 1)[1] if "%" in imm else ""
        gw = str(r.get("gateway") or "")
        if not iface and "%" in gw:
            iface = gw.split("%", 1)[1]
        key = iface or gw
        if key in seen:
            continue
        seen.add(key)
        d = dhcp.get(iface, {})
        out.append({"interface": iface or None, "port": _port_of(iface) if iface else None,
                    "gateway": gw.split("%")[0] or d.get("gateway"), "address": addrs.get(iface) or
                    str(d.get("address") or "").split("/")[0] or None,
                    "distance": r.get("distance"), "active": _true(r.get("active")),
                    "dhcp": d.get("status"), "comment": r.get("comment")})
    # DHCP klient bez výchozí routy v inventuře (např. add-default-route=no) – i tak WAN kandidát
    for iface, d in dhcp.items():
        if iface not in seen and _true(d.get("add-default-route", "yes")) and d.get("status") == "bound":
            out.append({"interface": iface, "port": _port_of(iface), "gateway": d.get("gateway"),
                        "address": str(d.get("address") or "").split("/")[0] or None, "distance": None,
                        "active": False, "dhcp": d.get("status"), "comment": None})
    return out


def site_infra(devices: list[dict], invs: dict[str, dict]) -> dict:
    ids = [str(d["id"]) for d in devices]
    dev = {str(d["id"]): d for d in devices}
    owner: dict[str, str] = {}
    by_ident: dict[str, str] = {}
    for did in ids:
        inv = invs.get(did, {})
        for i in _rows(inv, "interfaces"):
            if i.get("mac-address"):
                owner[str(i["mac-address"]).upper()] = did
        for b in _rows(inv, "stp_bridges"):
            if b.get("current-mac-address"):
                owner[str(b["current-mac-address"]).upper()] = did
        if dev[did].get("identity"):
            by_ident[str(dev[did]["identity"])] = did

    # spoje mezi spravovanými zařízeními. MNDP (broadcast) projde bridgi, takže zařízení na portu „vidí“
    # všechna zařízení za ním, ne jen přímého souseda. Přímý spoj A–B: B je vidět z A na portu P, A z B na
    # portu Q a množiny za P a za Q se nepřekrývají. LLDP/CDP jsou vždy jen přímí sousedé.
    seen: dict[tuple[str, str], set[str]] = {}
    lldp: set[tuple[str, str, str]] = set()
    externals: list[dict] = []
    ext_seen: set[tuple] = set()
    has_nb = set()
    for a in ids:
        for n in _rows(invs.get(a, {}), "neighbors"):
            has_nb.add(a)
            b = owner.get(str(n.get("mac-address") or "").upper()) or by_ident.get(str(n.get("identity") or ""))
            port = _port_of(n.get("interface"))
            if b and b != a:
                seen.setdefault((a, port), set()).add(b)
                if re.search(r"lldp|cdp", str(n.get("discovered-by") or "")):
                    lldp.add((a, port, b))
            elif not b:
                key = (a, port, str(n.get("mac-address") or n.get("identity")))
                if key not in ext_seen:
                    ext_seen.add(key)
                    externals.append({"parent": a, "port": port, "label": n.get("identity") or n.get("mac-address"),
                                      "platform": n.get("board") or n.get("platform"), "address": n.get("address")})
    ports: dict[tuple[str, str], str] = {}   # (a, b) → port na a
    for (a, p), bs in seen.items():
        for b in bs:
            if (a, p, b) in lldp:
                ports[(a, b)] = p
                continue
            for (b2, q), back in seen.items():
                if b2 == b and a in back and not (bs & back):
                    ports[(a, b)] = p
                    ports.setdefault((b, a), q)
    # zařízení bez dat o sousedech (offline): k pozorovateli, který ho vidí jako jediného za portem
    for b in ids:
        if b in has_nb or any(x == b for _a, x in ports):
            continue
        obs = sorted(((len(bs), a, p) for (a, p), bs in seen.items() if b in bs))
        if obs and obs[0][0] == 1:
            ports[(obs[0][1], b)] = obs[0][2]
    # porty, za kterými je víc zařízení a žádné přímo: nespravovaný switch („hub“) mezi nimi
    hub_of: dict[str, str] = {}
    hubs: dict[str, dict] = {}
    parent_uf: dict[str, str] = {}

    def find(x: str) -> str:
        while parent_uf.setdefault(x, x) != x:
            x = parent_uf[x]
        return x

    unexplained = {(a, p): bs for (a, p), bs in seen.items() if len(bs) > 1 and not any((a, b) in ports and ports[(a, b)] == p for b in bs)}
    for (a, p), bs in unexplained.items():
        for b in bs:
            if b not in has_nb or any(k[0] == b and a in v for k, v in unexplained.items()):
                parent_uf[find(a)] = find(b)
    groups: dict[str, set[str]] = {}
    for (a, p), bs in unexplained.items():
        for x in {a} | {b for b in bs if find(b) == find(a)}:
            groups.setdefault(find(a), set()).add(x)
    hub_port: dict[str, str | None] = {}
    for k, members in groups.items():
        if len(members) < 2:
            continue
        hid = f"hub-{len(hubs) + 1}"
        hubs[hid] = {"members": sorted(members)}
        for m in members:
            hub_of[m] = hid
            hub_port[m] = next((p for (a, p) in unexplained if a == m), None)
    adj: dict[str, set[str]] = {d: set() for d in list(ids) + list(hubs)}
    for a, b in ports:
        if hub_of.get(a) and hub_of.get(a) == hub_of.get(b) and ports.get((a, b)) == hub_port.get(a):
            continue   # spojení přes hub, ne přímé
        adj[a].add(b)
        adj[b].add(a)
    for hid, h in hubs.items():
        for m in h["members"]:
            adj[hid].add(m)
            adj[m].add(hid)
    infos: dict[str, dict] = {}
    for did in ids:
        inv = invs.get(did, {})
        res = _obj(inv, "resource")
        mon = _rows(inv, "stp_bridges")
        prio = next((b.get("priority") for b in _rows(inv, "bridges") if b.get("priority")), None)
        infos[did] = {
            "id": did, "label": dev[did].get("identity") or dev[did].get("serial"), "model": dev[did].get("model"),
            "role": dev[did].get("role"), "state": dev[did].get("state"), "version": dev[did].get("ros_version"),
            "cpu": int(res["cpu-load"]) if str(res.get("cpu-load", "")).isdigit() else None,
            "mem": _pct(res.get("free-memory"), res.get("total-memory")),
            "poe_w": round(sum(float(p.get("poe-out-power") or 0) for p in _rows(inv, "poe_monitor")), 1),
            "stp_root": any(_true(b.get("root-bridge")) for b in mon),
            "stp_priority": int(str(prio), 16) if prio and str(prio).startswith("0x") else (int(prio) if str(prio or "").isdigit() else None),
            "ports": _ports(inv), "wans": _wans(inv),
            "parent": None, "level": 0, "uplink_port": None, "parent_port": None,
        }

    # vlastní WAN jen u brány (role gateway) nebo na routovaném fyzickém portu; DHCP klient / výchozí
    # routa na bridgi či VLAN u switche a AP je jen adresa správy – brána lokality je pak mimo správu
    bridged = {did: {str(b.get("name")) for b in _rows(invs.get(did, {}), "bridges")} for did in ids}
    upstream: list[dict] = []
    for did in ids:
        own = []
        for w in infos[did]["wans"]:
            iface = str(w.get("interface") or "")
            if infos[did]["role"] == "gateway" or (PHYS_RE.match(iface) and iface not in bridged[did]) or \
                    re.match(r"^(pppoe|lte|wg|l2tp|sstp|ovpn)", iface) and not iface.startswith("mtc"):
                own.append(w)
            elif w.get("gateway"):
                rp = next((str(m.get("root-port")) for m in _rows(invs.get(did, {}), "stp_bridges")
                           if m.get("root-port") and str(m.get("name")) == iface), None)
                upstream.append(dict(w, port=rp or w.get("port"), device_id=did))
        infos[did]["wans"] = own
    # STP root: korunka jen tomu, koho jako root vidí i ostatní (nebo je zařízení v lokalitě samo)
    st = stp.site_stp(devices, invs)
    followers: dict[str, int] = {}
    for b in st["bridges"]:
        if b["root_device"] and b["protocol"] != "none":
            followers[b["root_device"]] = followers.get(b["root_device"], 0) + 1
    self_roots = [d for d in ids if infos[d]["stp_root"]]
    for did in ids:
        lab = infos[did]["label"]
        infos[did]["stp_root"] = infos[did]["stp_root"] and (followers.get(lab, 0) >= 2 or len(self_roots) == 1)
    lonely = [infos[d]["label"] for d in self_roots if not infos[d]["stp_root"]]
    for hid, h in hubs.items():
        infos[hid] = {"id": hid, "kind": "hub", "label": "Nespravovaný switch / síť", "model": None,
                      "role": "hub", "state": "adopted", "version": None, "cpu": None, "mem": None, "poe_w": 0,
                      "stp_root": False, "stp_priority": None, "ports": [], "wans": [], "members": h["members"],
                      "parent": None, "level": 0, "uplink_port": None, "parent_port": None}

    # kořen: gateway → zařízení s WAN → hub (síť nad zařízeními) → STP root → první
    def rank(did: str) -> tuple:
        i = infos[did]
        return (i["role"] != "gateway", not i["wans"], i.get("kind") != "hub", not i["stp_root"], i["state"] != "adopted",
                i["label"] or "")

    def link_ports(a: str, b: str) -> tuple:
        if infos[a].get("kind") == "hub":
            return None, hub_port.get(b)
        if infos[b].get("kind") == "hub":
            return hub_port.get(a), None
        return ports.get((a, b)), ports.get((b, a))

    order: list[str] = []
    visited: set[str] = set()
    for root in sorted(infos, key=rank):
        if root in visited:
            continue
        visited.add(root)
        q = deque([root])
        while q:
            a = q.popleft()
            order.append(a)
            for b in sorted(adj[a], key=lambda x: (ROLE_ORDER.get(infos[x]["role"], 9), infos[x]["label"] or "")):
                if b in visited:
                    continue
                visited.add(b)
                pp, up = link_ports(a, b)
                infos[b].update(parent=a, level=infos[a]["level"] + 1, parent_port=pp, uplink_port=up)
                q.append(b)

    wans = []
    for did in order:
        if infos[did]["parent"] is None:
            for w in infos[did]["wans"]:
                wans.append(dict(w, device_id=did))
    if not wans and upstream:
        # brána mimo správu (např. router zákazníka): karta nad kořenem stromu
        root = order[0] if order else None
        gws = sorted({u["gateway"] for u in upstream if u.get("gateway")})
        u0 = next((u for u in upstream if hub_of.get(u["device_id"]) == root or u["device_id"] == root), upstream[0])
        wans.append({"interface": None, "port": None if root and infos[root].get("kind") == "hub" else u0.get("port"),
                     "gateway": ", ".join(gws), "address": None, "distance": None, "active": True, "dhcp": None,
                     "comment": None, "device_id": root, "upstream": True})
    warnings = [w for w in st["warnings"] if w["severity"] == "warning"]
    if len(lonely) > 1 or (lonely and any(i["stp_root"] for i in infos.values())):
        warnings.append({"severity": "warning", "text": f"STP: {', '.join(lonely)} se považuje za root jen sám pro sebe – "
                         "BPDU mezi zařízeními neprochází (jiný protokol STP, vypnuté STP na mezilehlém switchi nebo "
                         "filtrace) – riziko smyčky"})
    for hid, h in hubs.items():
        names_h = [infos[m]["label"] for m in h["members"]]
        warnings.append({"severity": "info", "text": f"{', '.join(names_h)} jsou propojené přes nespravovaný switch nebo "
                         "síť (vidí se navzájem na stejném portu) – zapni LLDP (/ip neighbor discovery-settings "
                         "protocol=cdp,lldp,mndp) pro přesnou mapu"})
    if st["blocked"]:
        warnings.append({"severity": "info", "text": f"STP blokuje {len(st['blocked'])} port(y) – v síti je redundantní cesta "
                                                    "(v pořádku, pokud je záměrná)"})
    for did in ids:
        if infos[did]["state"] == "offline":
            warnings.append({"severity": "warning", "text": f"{infos[did]['label']}: offline – spoj k němu je přerušený"})
    # nespravovaní sousedé vidění přes hub jen jednou (pod hubem)
    ext_out, ext_keys = [], set()
    for e in externals:
        par = hub_of.get(e["parent"]) if hub_port.get(e["parent"]) == e["port"] else None
        k = (par or e["parent"], e["label"])
        if k in ext_keys:
            continue
        ext_keys.add(k)
        ext_out.append(dict(e, parent=par, port=None) if par else e)
    return {"nodes": [infos[d] for d in order], "wans": wans, "externals": ext_out, "warnings": warnings}


ROLE_ORDER = {"gateway": 0, "switch": 1, "ptp": 2, "ap": 3, "unknown": 4}
