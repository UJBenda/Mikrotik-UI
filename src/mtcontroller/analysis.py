"""Analýza inventury: shoda s cílovým VLAN modelem a návrh migrace.

Cílový model: jeden bridge s vlan-filtering=yes, tagování v
/interface bridge vlan, přístupové porty přes pvid, L3 přes VLAN rozhraní
na bridge, ingress-filtering zapnutý.

Vše je čisté (bez I/O) – vstupem je slovník sekcí z inventory.collect().
Migrační plán VLAN je zatím jen náhled (dry-run): spouštět ho controller
neumí, dokud ho neověříme v laboratoři na všech typech zařízení.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from typing import Any

from .routeros import q

PHYSICAL_TYPES = {"ether", "wlan", "wifi", "sfp", "sfp-sfpplus", "lte", "ppp-out"}


@dataclass
class Finding:
    code: str
    severity: str  # "error" | "warning" | "info"
    text: str
    objects: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "text": self.text, "objects": self.objects}


def _rows(inv: dict, key: str) -> list[dict]:
    v = inv.get(key)
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def _enabled(r: dict) -> bool:
    return str(r.get("disabled", "false")).lower() not in ("true", "yes")


def _static(r: dict) -> bool:
    return str(r.get("dynamic", "false")).lower() not in ("true", "yes")


def _split(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    return [x for x in str(v or "").split(",") if x]


def _vids(v: Any) -> set[int]:
    out: set[int] = set()
    for part in _split(v):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            if a.isdigit() and b.isdigit():
                out.update(range(int(a), int(b) + 1))
        elif part.isdigit():
            out.add(int(part))
    return out


@dataclass
class VlanReport:
    status: str  # "conforming" | "simple" | "legacy" | "unknown"
    findings: list[Finding]
    summary: dict

    def as_dict(self) -> dict:
        return {"status": self.status, "findings": [f.as_dict() for f in self.findings], "summary": self.summary}


def analyze_vlan(inv: dict) -> VlanReport:
    if "bridges" not in inv or "interfaces" not in inv:
        return VlanReport("unknown", [Finding("no_data", "info", "Inventura zatím neproběhla.")], {})
    ifaces = {r.get("name"): r for r in _rows(inv, "interfaces")}
    bridges = [b for b in _rows(inv, "bridges") if _enabled(b)]
    bridge_names = {b.get("name") for b in bridges}
    ports = [p for p in _rows(inv, "bridge_ports") if _enabled(p) and _static(p)]
    vlans = [v for v in _rows(inv, "vlans") if _enabled(v)]
    bvlans = [v for v in _rows(inv, "bridge_vlans") if _enabled(v) and _static(v)]
    vlan_names = {v.get("name") for v in vlans}
    f: list[Finding] = []

    used_bridges = {p.get("bridge") for p in ports} | {v.get("interface") for v in vlans if v.get("interface") in bridge_names}
    active_bridges = [b for b in bridges if b.get("name") in used_bridges]
    if len(active_bridges) > 1:
        f.append(Finding("multiple_bridges", "warning",
                         "Více bridgů s porty – cílový model je jeden bridge s vlan-filtering.",
                         sorted(str(b.get("name")) for b in active_bridges)))

    for p in ports:
        if p.get("interface") in bridge_names:
            f.append(Finding("nested_bridge", "error", "Bridge je zapojený jako port jiného bridge.",
                             [f"{p.get('interface')} → {p.get('bridge')}"]))
        if p.get("interface") in vlan_names:
            f.append(Finding("vlan_iface_bridged", "warning",
                             "VLAN rozhraní je portem bridge (starý způsob VLAN přes bridge).",
                             [f"{p.get('interface')} → {p.get('bridge')}"]))

    for v in vlans:
        parent = v.get("interface")
        ptype = (ifaces.get(parent) or {}).get("type")
        if parent not in bridge_names and ptype in PHYSICAL_TYPES:
            in_bridge = any(p.get("interface") == parent for p in ports)
            f.append(Finding("vlan_on_physical", "warning" if in_bridge else "info",
                             "VLAN rozhraní na fyzickém portu" +
                             (", který je zároveň v bridge – VLAN obchází bridge." if in_bridge else "."),
                             [f"{v.get('name')} (vid {v.get('vlan-id')}) na {parent}"]))

    for b in bridges:
        name = b.get("name")
        vf = str(b.get("vlan-filtering", "false")).lower() in ("true", "yes")
        bvl = [v for v in vlans if v.get("interface") == name]
        if not vf and bvl:
            f.append(Finding("bridge_vlan_without_filtering", "warning",
                             "Na bridge jsou VLAN rozhraní, ale vlan-filtering je vypnutý – tagovaný provoz se nefiltruje.",
                             [str(name)]))
        if vf:
            tagged_vids: set[int] = set()
            for bv in bvlans:
                if bv.get("bridge") == name and name in _split(bv.get("tagged")):
                    tagged_vids |= _vids(bv.get("vlan-ids"))
            for v in bvl:
                vid = int(str(v.get("vlan-id") or 0)) if str(v.get("vlan-id") or "").isdigit() else 0
                if vid and vid not in tagged_vids:
                    f.append(Finding("bridge_not_tagged", "error",
                                     f"VLAN {vid} má rozhraní na bridge, ale bridge v ní není tagovaný – L3 v této VLAN nefunguje.",
                                     [f"{v.get('name')} na {name}"]))
            for p in ports:
                if p.get("bridge") == name and str(p.get("ingress-filtering", "true")).lower() in ("false", "no"):
                    f.append(Finding("ingress_filtering_off", "warning",
                                     "Port bez ingress-filtering – riziko VLAN hoppingu.",
                                     [f"{p.get('interface')} ({name})"]))

    legacy_codes = {"multiple_bridges", "nested_bridge", "vlan_iface_bridged", "bridge_vlan_without_filtering"}
    has_vlans = bool(vlans or bvlans)
    if any(x.code in legacy_codes or (x.code == "vlan_on_physical" and x.severity == "warning") for x in f):
        status = "legacy"
    elif not has_vlans:
        status = "simple"
    else:
        status = "conforming"
    if status == "legacy":
        f.append(Finding("hw_offload_note", "info",
                         "Před migrací ověř hardwarový offload: na některých starších čipech přepne "
                         "vlan-filtering provoz na CPU (viz dokumentace MikroTik Bridge Hardware Offloading)."))
    summary = {"bridges": len(active_bridges), "ports": len(ports), "vlan_interfaces": len(vlans),
               "bridge_vlans": len(bvlans)}
    return VlanReport(status, f, summary)


# --- návrh migrace (dry-run) --------------------------------------------------------


@dataclass
class MigrationPlan:
    ok: bool
    target_bridge: str
    steps: list[str]
    script: str
    blockers: list[str]
    warnings: list[str]

    def as_dict(self) -> dict:
        return {"ok": self.ok, "target_bridge": self.target_bridge, "steps": self.steps,
                "script": self.script, "blockers": self.blockers, "warnings": self.warnings}


def plan_vlan_migration(inv: dict, target: str | None = None) -> MigrationPlan:
    """Převod vzoru „bridge na každou VLAN + VLAN rozhraní na trunku" na jeden
    bridge s vlan-filtering. Složitější zapojení vrací blockers (ruční řešení)."""
    bridges = [b for b in _rows(inv, "bridges") if _enabled(b)]
    bnames = [str(b.get("name")) for b in bridges]
    ports = [p for p in _rows(inv, "bridge_ports") if _enabled(p) and _static(p)]
    vlans = {str(v.get("name")): v for v in _rows(inv, "vlans") if _enabled(v)}
    addrs = [a for a in _rows(inv, "addresses") if _static(a)]
    dhcp = _rows(inv, "dhcp_servers")
    names_in_use = {str(r.get("name")) for r in _rows(inv, "interfaces")}
    blockers: list[str] = []
    warnings: list[str] = []
    if not bridges:
        return MigrationPlan(False, "", [], "", ["Zařízení nemá žádný bridge."], [])

    # cílový bridge: zadaný, nebo ten bez VLAN členů (nativní LAN), nebo první
    def members(b: str) -> list[dict]:
        return [p for p in ports if p.get("bridge") == b]

    if target is None:
        native = [b for b in bnames if not any(str(p.get("interface")) in vlans for p in members(b))]
        target = native[0] if native else bnames[0]
    if target not in bnames:
        return MigrationPlan(False, target, [], "", [f"Bridge {target} neexistuje."], [])

    trunk_tags: dict[str, set[int]] = {}   # fyzický port -> tagované VLAN
    access: dict[str, int] = {}            # port -> pvid
    bridge_vid: dict[str, int] = {}        # původní bridge -> VLAN
    remove_vlan_ifaces: list[str] = []
    for b in bnames:
        vmem = [vlans[str(p.get("interface"))] for p in members(b) if str(p.get("interface")) in vlans]
        vids = {int(str(v.get("vlan-id"))) for v in vmem if str(v.get("vlan-id", "")).isdigit()}
        if len(vids) > 1:
            blockers.append(f"Bridge {b} spojuje víc VLAN ({sorted(vids)}) – nutné řešit ručně.")
            continue
        vid = vids.pop() if vids else 1
        if b != target and vid == 1:
            blockers.append(f"Bridge {b} nemá VLAN a není cílový – nejasné, kam ho sloučit.")
            continue
        bridge_vid[b] = vid
        for v in vmem:
            parent = str(v.get("interface"))
            trunk_tags.setdefault(parent, set()).add(vid)
            remove_vlan_ifaces.append(str(v.get("name")))
        for p in members(b):
            iface = str(p.get("interface"))
            if iface in vlans or iface in bnames:
                continue
            if iface in access and access[iface] != vid:
                blockers.append(f"Port {iface} je ve více bridgích.")
            access[iface] = vid
    for v in vlans.values():
        if str(v.get("name")) not in remove_vlan_ifaces and str(v.get("interface")) in bnames:
            continue  # VLAN rozhraní na bridge (už cílový model) – ponechat
        if str(v.get("name")) not in remove_vlan_ifaces and str(v.get("interface")) not in bnames:
            warnings.append(f"VLAN rozhraní {v.get('name')} na {v.get('interface')} není v žádném bridge – zůstává beze změny.")
    for port in trunk_tags:
        if port in access and access[port] != 1:
            blockers.append(f"Port {port} je trunk i přístupový port jiné VLAN.")
    if blockers:
        return MigrationPlan(False, target, [], "", blockers, warnings)

    steps: list[str] = []
    lines = ["# NAHLED migrace VLAN - vygenerovano controllerem, NESPOUSTET bez kontroly",
             "# Doporuceni: provadet z konzole/MAC-Winboxu, ne pres sit, ktera se prave meni.", "{"]
    vids = sorted({v for v in bridge_vid.values()} | {v for s in trunk_tags.values() for v in s})
    new_vlan_iface: dict[int, str] = {}
    for vid in vids:
        if vid == 1:
            continue
        name = f"vlan{vid}"
        # nové rozhraní vzniká dřív, než se smaže staré – jméno nesmí kolidovat ani se starým
        while name in names_in_use:
            name += "-br"
        new_vlan_iface[vid] = name
    # 1) porty do cílového bridge s pvid
    for iface, vid in sorted(access.items()):
        steps.append(f"Port {iface}: do bridge {target}, pvid {vid}" + (" (trunk)" if iface in trunk_tags else ""))
        lines.append(f"/interface bridge port set [find interface={q(iface)}] bridge={q(target)} pvid={vid}")
    for iface in sorted(trunk_tags):
        if iface not in access:
            steps.append(f"Port {iface}: přidat do bridge {target} jako trunk")
            lines.append(f"/interface bridge port add bridge={q(target)} interface={q(iface)} pvid=1 comment=\"mtc: migrace VLAN\"")
    # 2) tabulka VLAN na bridge
    for vid in vids:
        if vid == 1:
            continue
        tagged = [target] + sorted(p for p, s in trunk_tags.items() if vid in s)
        untagged = sorted(p for p, v in access.items() if v == vid and p not in trunk_tags)
        steps.append(f"VLAN {vid}: tagged {', '.join(tagged)}" + (f"; untagged {', '.join(untagged)}" if untagged else ""))
        cmd = f"/interface bridge vlan add bridge={q(target)} vlan-ids={vid} tagged={q(','.join(tagged))}"
        if untagged:
            cmd += f" untagged={q(','.join(untagged))}"
        lines.append(cmd + ' comment="mtc: migrace VLAN"')
    # 3) L3: nová VLAN rozhraní na bridge, přesun adres a DHCP
    for b, vid in sorted(bridge_vid.items()):
        if b == target:
            continue
        nv = new_vlan_iface[vid]
        steps.append(f"Vytvořit {nv} (vid {vid}) na {target}, přesunout adresy a DHCP z {b}")
        lines.append(f"/interface vlan add name={q(nv)} interface={q(target)} vlan-id={vid} comment=\"mtc: migrace VLAN\"")
        for a in addrs:
            if a.get("interface") == b:
                lines.append(f"/ip address set [find address={q(str(a.get('address')))} interface={q(b)}] interface={q(nv)}")
        for d in dhcp:
            if d.get("interface") == b:
                lines.append(f"/ip dhcp-server set [find name={q(str(d.get('name')))}] interface={q(nv)}")
    # 4) úklid starých objektů
    for v in sorted(set(remove_vlan_ifaces)):
        lines.append(f"/interface bridge port remove [find interface={q(v)}]")
        lines.append(f"/interface vlan remove [find name={q(v)}]")
    for b in sorted(bridge_vid):
        if b != target:
            steps.append(f"Odstranit bridge {b}")
            lines.append(f"/interface bridge remove [find name={q(b)}]")
    # 5) až nakonec zapnout filtrování
    steps.append(f"Zapnout vlan-filtering na {target}")
    lines.append(f"/interface bridge set [find name={q(target)}] vlan-filtering=yes")
    lines.append("}")
    warnings.append("Firewall, interface-listy, DHCP relay a další odkazy na odstraněné bridge/VLAN rozhraní "
                    "controller zatím nekontroluje – zkontroluj je ručně.")
    return MigrationPlan(True, target, steps, "\n".join(lines) + "\n", [], warnings)


# --- NTP -----------------------------------------------------------------------------


def ntp_status(inv: dict) -> dict:
    c = inv.get("ntp_client") if isinstance(inv.get("ntp_client"), dict) else {}
    s = inv.get("ntp_server") if isinstance(inv.get("ntp_server"), dict) else {}
    clock = inv.get("clock") if isinstance(inv.get("clock"), dict) else {}
    return {
        "client_enabled": str(c.get("enabled", "false")).lower() in ("true", "yes"),
        "servers": _split(c.get("servers")),
        "status": c.get("status"),
        "server_enabled": str(s.get("enabled", "false")).lower() in ("true", "yes"),
        "timezone": clock.get("time-zone-name"),
    }


def addresses(inv: dict) -> list[ipaddress.IPv4Interface]:
    out = []
    for a in _rows(inv, "addresses"):
        if not _enabled(a):
            continue
        try:
            out.append(ipaddress.ip_interface(str(a.get("address"))))
        except ValueError:
            pass
    return out


def reachable_address(server_inv: dict, client_inv: dict) -> str | None:
    """Adresa serveru ve stejné síti jako některá adresa klienta."""
    srv = addresses(server_inv)
    for c in addresses(client_inv):
        for s in srv:
            if s.ip != c.ip and s.ip in c.network and not s.ip.is_loopback:
                return str(s.ip)
    return None
