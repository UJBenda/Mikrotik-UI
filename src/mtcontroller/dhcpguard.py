"""Sítě lokality (jako UniFi Settings → Networks) a ochrana proti cizím DHCP
serverům (DHCP snooping s důvěryhodnými porty + DHCP alert na routerech).

DHCP snooping na bridgi RouterOS zahodí odpovědi DHCP serveru (OFFER/ACK),
které přijdou z portu bez `trusted=yes`. Důvěryhodný musí být každý port,
za kterým leží *cesta k legitimnímu DHCP serveru* – na každém switchi jiný.
Proto se plán počítá z topologie lokality:

1. zdroje DHCP: DHCP servery na spravovaných zařízeních, DHCP relay (relay
   samotný i jeho cílový server, dohledaný podle IP mezi zařízeními a klienty),
   DHCP klienti zařízení (vlastní správa switche/AP) a ručně zadané zdroje
   (např. Windows DHCP na portu, který controller nezná),
2. graf spravovaných zařízení ze sousedů MNDP/LLDP (port na obou koncích),
3. pro každé zařízení: první port na cestě ke každému zdroji = trusted.

Pokud cesta k některému zdroji není známá, plán pro dané zařízení se
nevytvoří – zapnutý snooping by DHCP odřízl. Existující trusted porty se
nikdy neruší. Snooping se zapíná jen na bridgích, kudy DHCP opravdu teče.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections import deque
from typing import Any

from . import health
from .ports import _vkey
from .routeros import q

PHYS_RE = re.compile(r"^(ether|sfp|combo|qsfp|bond|wlan|wifi)")
PORT_NAME_RE = re.compile(r"^[A-Za-z0-9._/+-]{1,64}$")
MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
ROGUE_LOG = ':log warning "mtc: cizi DHCP server"'
HW_SNOOPING = re.compile(r"^(CRS3|CRS5|CCR2116|CCR2216)", re.I)


def _rows(inv: dict, key: str) -> list[dict]:
    v = inv.get(key)
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def _true(v: Any) -> bool:
    return str(v).lower() in ("true", "yes")


def _label(d: dict) -> str:
    return str(d.get("identity") or d.get("serial") or d.get("id"))


def _vids(spec: Any) -> set[int]:
    out: set[int] = set()
    for part in str(spec or "").split(","):
        part = part.strip()
        if "-" in part:
            lo, _, hi = part.partition("-")
            if lo.isdigit() and hi.isdigit():
                out.update(range(int(lo), min(int(hi), 4094) + 1))
        elif part.isdigit():
            out.add(int(part))
    return out


def iface_bridge(inv: dict, iface: str) -> str | None:
    """Bridge, na kterém rozhraní leží: bridge sám, VLAN rozhraní na bridgi, nebo port bridge."""
    bridges = {str(b.get("name")) for b in _rows(inv, "bridges") if not _true(b.get("disabled"))}
    if iface in bridges:
        return iface
    for v in _rows(inv, "vlans"):
        if v.get("name") == iface and str(v.get("interface")) in bridges:
            return str(v.get("interface"))
    for bp in _rows(inv, "bridge_ports"):
        if bp.get("interface") == iface and not _true(bp.get("disabled")):
            return str(bp.get("bridge"))
    return None


def _wan_ifaces(inv: dict) -> set[str]:
    out = {str(c.get("interface")) for c in _rows(inv, "dhcp_client") if not _true(c.get("disabled"))}
    out |= {str(m.get("interface")) for m in _rows(inv, "if_list_members")
            if str(m.get("list")).upper() == "WAN" and not _true(m.get("disabled"))}
    return out


# --- sítě lokality ------------------------------------------------------------------------------

def site_networks(devices: list[dict], invs: dict[str, dict]) -> dict:
    """Tabulka sítí (L3 rozhraní s adresou) + nastavení bridgů napříč lokalitou."""
    nets: list[dict] = []
    vlan_on: dict[int, set[str]] = {}
    for d in devices:
        inv = invs.get(str(d["id"]), {})
        for v in _rows(inv, "bridge_vlans"):
            if not _true(v.get("disabled")):
                for vid in _vids(v.get("vlan-ids")):
                    vlan_on.setdefault(vid, set()).add(str(d["id"]))
    for d in devices:
        did = str(d["id"])
        inv = invs.get(did, {})
        types = {str(i.get("name")): str(i.get("type")) for i in _rows(inv, "interfaces")}
        vlans = {str(v.get("name")): v for v in _rows(inv, "vlans")}
        bridges = {str(b.get("name")): b for b in _rows(inv, "bridges")}
        wan = _wan_ifaces(inv)
        usage = inv.get("dhcp_usage") if isinstance(inv.get("dhcp_usage"), list) else health.dhcp_pools(inv)
        usage_by = {str(u.get("server")): u for u in usage if isinstance(u, dict)}
        servers = [s for s in _rows(inv, "dhcp_servers") if not _true(s.get("disabled"))]
        relays = [r for r in _rows(inv, "dhcp_relays") if not _true(r.get("disabled"))]
        for a in _rows(inv, "addresses"):
            iface = str(a.get("interface") or "")
            if _true(a.get("disabled")) or _true(a.get("dynamic")) or iface in wan:
                continue
            if types.get(iface) not in ("vlan", "bridge", "ether", "bond") or iface.startswith("mtc"):
                continue
            try:
                ipi = ipaddress.ip_interface(str(a.get("address")))
            except ValueError:
                continue
            if ipi.version != 4 or ipi.network.prefixlen >= 31:
                continue
            vid, tagged = None, False
            if iface in vlans:
                vid = int(vlans[iface]["vlan-id"]) if str(vlans[iface].get("vlan-id", "")).isdigit() else None
                tagged = True
            elif iface in bridges and _true(bridges[iface].get("vlan-filtering")):
                vid = int(bridges[iface].get("pvid") or 1) if str(bridges[iface].get("pvid") or "1").isdigit() else 1
            srv = next((s for s in servers if s.get("interface") == iface), None)
            rel = next((r for r in relays if r.get("interface") == iface), None)
            dhcp: dict[str, Any] = {"mode": None}
            if srv:
                u = usage_by.get(str(srv.get("name"))) or {}
                dhcp = {"mode": "server", "name": srv.get("name"), "used": u.get("used"), "size": u.get("size")}
            elif rel:
                dhcp = {"mode": "relay", "name": rel.get("name"), "target": rel.get("dhcp-server")}
            comment = (vlans.get(iface) or {}).get("comment") or ""
            nets.append({
                "name": iface, "comment": comment, "vlan_id": vid, "tagged": tagged,
                "router_id": did, "router": _label(d), "subnet": str(ipi.network), "gateway": str(ipi.ip),
                "dhcp": dhcp,
                "available": (dhcp["size"] - dhcp["used"]) if dhcp.get("size") is not None and dhcp.get("used") is not None else None,
                "devices": len(vlan_on.get(vid, set())) if vid else None,
            })
    nets.sort(key=lambda n: (n["vlan_id"] if n["vlan_id"] is not None else 1 << 20, n["router"], n["subnet"]))
    settings = []
    for d in devices:
        inv = invs.get(str(d["id"]), {})
        for b in _rows(inv, "bridges"):
            if _true(b.get("disabled")):
                continue
            nports = sum(1 for bp in _rows(inv, "bridge_ports") if bp.get("bridge") == b.get("name"))
            settings.append({
                "device_id": str(d["id"]), "device": _label(d), "bridge": b.get("name"), "ports": nports,
                "protocol": b.get("protocol-mode"), "vlan_filtering": _true(b.get("vlan-filtering")),
                "igmp_snooping": _true(b.get("igmp-snooping")), "dhcp_snooping": _true(b.get("dhcp-snooping")),
                "option82": _true(b.get("add-dhcp-option82")),
                "trusted": sorted(str(bp.get("interface")) for bp in _rows(inv, "bridge_ports")
                                  if bp.get("bridge") == b.get("name") and _true(bp.get("trusted"))),
            })
    warnings = []
    protos = {s["protocol"] for s in settings if s["ports"] > 1}
    if len(protos) > 1:
        warnings.append(f"bridge na lokalitě běží s různými protokoly STP: {', '.join(sorted(str(p) for p in protos))}")
    if any(s["protocol"] == "none" and s["ports"] > 2 for s in settings):
        warnings.append("některý bridge s více porty má STP vypnuté – smyčka shodí síť")
    subnets: dict[str, list[str]] = {}
    for n in nets:
        subnets.setdefault(n["subnet"], []).append(n["router"])
    for sn, who in subnets.items():
        if len(who) > 1:
            warnings.append(f"podsíť {sn} je nastavená na více zařízeních: {', '.join(who)}")
    return {"networks": nets, "bridges": settings, "warnings": warnings}


# --- graf lokality a cesty ----------------------------------------------------------------------

def _phys(iface: Any) -> str | None:
    for p in re.split(r"[,/]", str(iface or "")):
        if PHYS_RE.match(p):
            return p
    return None


def site_graph(devices: list[dict], invs: dict[str, dict]) -> dict[str, dict[str, str | None]]:
    """adj[a][b] = port na zařízení a vedoucí k sousedovi b (None = port neznámý)."""
    mac_owner: dict[str, str] = {}
    ident_owner: dict[str, str] = {}
    for d in devices:
        did = str(d["id"])
        for i in _rows(invs.get(did, {}), "interfaces"):
            if i.get("mac-address"):
                mac_owner[str(i["mac-address"]).upper()] = did
        if d.get("identity"):
            ident_owner[str(d["identity"])] = did
    adj: dict[str, dict[str, str | None]] = {str(d["id"]): {} for d in devices}
    for d in devices:
        a = str(d["id"])
        for n in _rows(invs.get(a, {}), "neighbors"):
            b = mac_owner.get(str(n.get("mac-address") or "").upper()) or ident_owner.get(str(n.get("identity") or ""))
            if not b or b == a:
                continue
            port = _phys(n.get("interface"))
            if port or b not in adj[a]:
                adj[a][b] = port or adj[a].get(b)
            # LLDP/MNDP hlásí i jméno portu na druhé straně
            remote = _phys(n.get("interface-name"))
            if remote and not adj[b].get(a):
                adj[b][a] = remote
            adj[b].setdefault(a, None)
    return adj


def first_hop(adj: dict[str, dict[str, str | None]], src: str, dst: str) -> tuple[bool, str | None]:
    """(cesta existuje, port na src). Port None při existující cestě = port neznámý."""
    if src == dst:
        return True, None
    prev: dict[str, str] = {src: src}
    dq = deque([src])
    while dq:
        x = dq.popleft()
        for y in adj.get(x, {}):
            if y not in prev:
                prev[y] = x
                if y == dst:
                    hop = y
                    while prev[hop] != src:
                        hop = prev[hop]
                    return True, adj[src].get(hop)
                dq.append(y)
    return False, None


# --- zdroje DHCP --------------------------------------------------------------------------------

def locate_ip(ip: str, devices: list[dict], invs: dict[str, dict], clients: dict[str, dict]) -> tuple[str, str | None] | None:
    """Kde je IP: (device_id, None) = spravované zařízení, (device_id, port) = klient na portu."""
    for d in devices:
        for a in _rows(invs.get(str(d["id"]), {}), "addresses"):
            if str(a.get("address") or "").split("/")[0] == ip:
                return str(d["id"]), None
    c = clients.get(ip)
    if c and c.get("device_id") and c.get("port"):
        return str(c["device_id"]), str(c["port"])
    return None


def dhcp_sources(devices: list[dict], invs: dict[str, dict], clients: dict[str, dict],
                 manual: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """Vrátí (zdroje, nedohledané). Zdroj: {device_id, port, label, only}; only = platí jen pro to zařízení."""
    names = {str(d["id"]): _label(d) for d in devices}
    out: list[dict] = []
    missing: list[dict] = []

    def add_ip(ip: str, label: str, only: str | None = None) -> None:
        loc = locate_ip(ip, devices, invs, clients)
        if loc:
            where = names.get(loc[0], "?") + (f" port {loc[1]}" if loc[1] else "")
            out.append({"device_id": loc[0], "port": loc[1], "label": f"{label} → {where}", "only": only})
        else:
            missing.append({"ip": ip, "label": label, "only": only})

    for d in devices:
        did = str(d["id"])
        inv = invs.get(did, {})
        for s in _rows(inv, "dhcp_servers"):
            if not _true(s.get("disabled")):
                out.append({"device_id": did, "port": None, "only": None,
                            "label": f"DHCP server {s.get('name')} ({s.get('interface')}) na {names[did]}"})
        for r in _rows(inv, "dhcp_relays"):
            if _true(r.get("disabled")):
                continue
            out.append({"device_id": did, "port": None, "only": None,
                        "label": f"DHCP relay {r.get('name')} ({r.get('interface')}) na {names[did]}"})
            for ip in str(r.get("dhcp-server") or "").split(","):
                ip = ip.strip()
                if ip:
                    add_ip(ip, f"DHCP server {ip} (cíl relay {r.get('name')} na {names[did]})")
        # vlastní adresa zařízení z DHCP (switch/AP ve správní VLAN) – jen pokud jde přes bridge
        for c in _rows(inv, "dhcp_client"):
            if _true(c.get("disabled")) or not c.get("dhcp-server") or not iface_bridge(inv, str(c.get("interface"))):
                continue
            add_ip(str(c["dhcp-server"]), f"DHCP server {c['dhcp-server']} pro {c.get('interface')} na {names[did]}", only=did)
    for m in manual or []:
        if str(m.get("device_id")) in names:
            out.append({"device_id": str(m["device_id"]), "port": m.get("port") or None, "only": None,
                        "label": str(m.get("label") or "ruční zdroj") + f" ({names[str(m['device_id'])]} {m.get('port') or ''})".rstrip()})
    # ruční zdroj s IP nahrazuje nedohledaný server (např. Windows DHCP za nespravovaným switchem)
    manual_ips = {str(m.get("ip")) for m in manual or [] if m.get("ip")}
    missing = [x for x in missing if x["ip"] not in manual_ips]
    return out, missing


# --- plán DHCP snoopingu -------------------------------------------------------------------------

def basis(inv: dict) -> str:
    """Otisk konfigurace, ze které plán vychází (bridge, trusted porty, DHCP alerty)."""
    data = {
        "br": sorted((str(b.get("name")), str(b.get("dhcp-snooping")), str(b.get("disabled")))
                     for b in _rows(inv, "bridges")),
        "bp": sorted((str(b.get("interface")), str(b.get("bridge")), str(b.get("trusted")))
                     for b in _rows(inv, "bridge_ports") if not _true(b.get("dynamic"))),
        "al": sorted((str(a.get("interface")), str(a.get("comment") or "")) for a in _rows(inv, "dhcp_alerts")),
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _version_lt(v: str, want: tuple[int, int]) -> bool:
    m = re.match(r"(\d+)\.(\d+)", str(v or ""))
    return bool(m) and (int(m.group(1)), int(m.group(2))) < want


def device_plan(d: dict, inv: dict, sources: list[dict], missing: list[dict], adj: dict,
                rogue_alerts: bool = True) -> dict:
    try:
        return _device_plan(d, inv, sources, missing, adj, rogue_alerts)
    except ValueError as e:  # jméno rozhraní, které nejde bezpečně vložit do skriptu
        return {"device_id": str(d["id"]), "device": _label(d), "status": "blocked", "trusted": [], "keep": [],
                "bridges": [], "alerts": [], "warnings": [], "reasons": [str(e)], "apply": "", "revert": "",
                "summary": ""}


def _device_plan(d: dict, inv: dict, sources: list[dict], missing: list[dict], adj: dict,
                 rogue_alerts: bool) -> dict:
    """Plán pro jedno zařízení: {status, trusted, keep, bridges, alerts, warnings, reasons, apply, revert, summary}."""
    did = str(d["id"])
    ports = {str(bp.get("interface")): bp for bp in _rows(inv, "bridge_ports")
             if not _true(bp.get("disabled")) and not _true(bp.get("dynamic"))}
    bridges = {str(b.get("name")): b for b in _rows(inv, "bridges") if not _true(b.get("disabled"))}
    out: dict[str, Any] = {"device_id": did, "device": _label(d), "status": "ok", "trusted": [], "keep": [],
                           "bridges": [], "alerts": [], "warnings": [], "reasons": [], "apply": "", "revert": "",
                           "summary": ""}
    if not bridges or not ports:
        out.update(status="skip", reasons=["zařízení nemá bridge s porty"])
        return out
    trusted: dict[str, list[str]] = {}
    unknown: list[str] = []
    local_bridges: set[str] = set()
    for s in sources:
        if s["only"] and s["only"] != did:
            continue
        if s["device_id"] == did:
            if s["port"]:
                trusted.setdefault(s["port"], []).append(s["label"])
            continue
        ok, hop = first_hop(adj, did, s["device_id"])
        if not ok:
            unknown.append(f"cesta k {s['label']} není známá (chybí soused MNDP/LLDP)")
        elif hop is None:
            unknown.append(f"port směrem k {s['label']} není známý")
        else:
            trusted.setdefault(hop, []).append(s["label"])
    for m in missing:
        if not m["only"] or m["only"] == did:
            unknown.append(f"{m['label']}: nevím, kde v síti je – zadej ho ručně jako zdroj")
    # lokální DHCP server / relay běží na bridgi (VLAN rozhraní) → snooping na tom bridgi
    for s in _rows(inv, "dhcp_servers") + _rows(inv, "dhcp_relays"):
        if not _true(s.get("disabled")):
            br = iface_bridge(inv, str(s.get("interface")))
            if br in bridges:
                local_bridges.add(br)
    # trusted jen porty v bridgi (routovaný uplink DHCP přes bridge nepouští, není co povolit)
    trusted = {p: why for p, why in trusted.items() if p in ports}
    enable = {str(ports[p].get("bridge")) for p in trusted} | local_bridges
    enable = {b for b in enable if b in bridges}
    out["keep"] = sorted((p for p, bp in ports.items() if _true(bp.get("trusted"))), key=_vkey)
    if unknown:
        out.update(status="blocked", reasons=unknown)
        return out
    if not enable:
        out.update(status="skip", reasons=["žádný bridge, kterým teče DHCP ze známého zdroje"])
        return out
    model = str(d.get("model") or (inv.get("resource") or {}).get("board-name") or "")
    if not HW_SNOOPING.match(model):
        out["warnings"].append(f"{model or 'model'}: hardwarová podpora DHCP snoopingu je dokumentovaná jen pro "
                               "CRS3xx/CRS5xx/CCR2116/CCR2216 – po zapnutí ověř, že porty mají dál HW offload (H)")
    if _version_lt(str(d.get("ros_version") or ""), (7, 17)) and any(
            str(p).startswith("bond") for p in ports if str(ports[p].get("bridge")) in enable):
        out["warnings"].append("RouterOS < 7.17: DHCP snooping nefunguje s HW offload bonding rozhraním")
    apply, revert = [], []
    for p in sorted(trusted, key=_vkey):
        if _true(ports[p].get("trusted")):
            continue
        out["trusted"].append({"port": p, "bridge": ports[p].get("bridge"), "why": trusted[p]})
        apply.append(f"/interface bridge port set [find interface={q(p)} dynamic=no] trusted=yes")
        revert.append(f"/interface bridge port set [find interface={q(p)} dynamic=no] trusted=no")
    for b in sorted(enable):
        on = _true(bridges[b].get("dhcp-snooping"))
        out["bridges"].append({"bridge": b, "now": on})
        if not on:
            # snooping až po nastavení trusted portů (opačně by na chvíli zahodil odpovědi serveru)
            apply.append(f"/interface bridge set [find name={q(b)}] dhcp-snooping=yes")
            revert.append(f"/interface bridge set [find name={q(b)}] dhcp-snooping=no")
    if rogue_alerts:
        have = {str(a.get("interface")) for a in _rows(inv, "dhcp_alerts")}
        macs = {str(i.get("name")): str(i.get("mac-address") or "") for i in _rows(inv, "interfaces")}
        for s in _rows(inv, "dhcp_servers"):
            iface = str(s.get("interface") or "")
            if _true(s.get("disabled")) or iface in have or not MAC_RE.match(macs.get(iface, "")):
                continue
            out["alerts"].append(iface)
            apply.append(f"/ip dhcp-server alert add interface={q(iface)} valid-server={macs[iface]} "
                         f"alert-timeout=1h on-alert={q(ROGUE_LOG)} comment=\"mtc\"")
            revert.append(f'/ip dhcp-server alert remove [find interface={q(iface)} comment="mtc"]')
            have.add(iface)
    if not apply:
        out.update(status="ok", summary="beze změny – snooping už je nastavený")
        return out
    parts = []
    if out["trusted"]:
        parts.append("trusted " + ", ".join(t["port"] for t in out["trusted"]))
    new_br = [b["bridge"] for b in out["bridges"] if not b["now"]]
    if new_br:
        parts.append("DHCP snooping na " + ", ".join(new_br))
    if out["alerts"]:
        parts.append("DHCP alert na " + ", ".join(out["alerts"]))
    out.update(status="plan", apply="\n".join(apply) + "\n", revert="\n".join(reversed(revert)) + "\n",
               summary="; ".join(parts))
    return out


def site_snooping(devices: list[dict], invs: dict[str, dict], clients: dict[str, dict],
                  policy: dict | None = None) -> dict:
    policy = policy or {}
    sources, missing = dhcp_sources(devices, invs, clients, policy.get("sources"))
    adj = site_graph(devices, invs)
    plans = [device_plan(d, invs.get(str(d["id"]), {}), sources, missing, adj, policy.get("rogue_alerts", True))
             for d in devices]
    rogue = []
    for d in devices:
        for a in _rows(invs.get(str(d["id"]), {}), "dhcp_alerts"):
            if a.get("unknown-server"):
                rogue.append({"device_id": str(d["id"]), "device": _label(d), "interface": a.get("interface"),
                              "servers": str(a["unknown-server"])})
    return {"sources": sources, "missing": missing, "devices": plans, "rogue": rogue, "policy": policy}


def validate_policy(data: dict, device_ids: set[str]) -> dict:
    srcs = []
    for s in data.get("sources") or []:
        if not isinstance(s, dict):
            raise ValueError("zdroj musí být objekt")
        did = str(s.get("device_id") or "")
        port = str(s.get("port") or "")
        label = str(s.get("label") or "ruční zdroj")[:80]
        if did not in device_ids:
            raise ValueError("zařízení zdroje nepatří do lokality")
        if not PORT_NAME_RE.match(port):
            raise ValueError("neplatné jméno portu")
        if not re.match(r"^[^\x00-\x1f\x7f]{1,80}$", label):
            raise ValueError("popis: 1–80 znaků bez řídicích znaků")
        ip = str(s.get("ip") or "")
        if ip:
            try:
                ip = str(ipaddress.IPv4Address(ip))
            except ValueError:
                raise ValueError("neplatná IP adresa zdroje") from None
        srcs.append({"device_id": did, "port": port, "label": label, "ip": ip or None})
    if len(srcs) > 50:
        raise ValueError("max. 50 ručních zdrojů")
    return {"sources": srcs, "rogue_alerts": bool(data.get("rogue_alerts", True))}
