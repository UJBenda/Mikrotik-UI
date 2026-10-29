"""VLAN lokality na adoptovaném switchi bez převzetí načisto (bez resetu).

Po adopci má zařízení bridge, ale o sítích lokality neví. Tenhle plán (jako změna portů,
s automatickým návratem) do VLAN tabulky bridge přidá všechny sítě lokality – na uplinku
tagovaně, aby provoz prošel dál – a zapne vlan-filtering. Pak už se porty jen přiřazují
(editor portu, profil „Trunk – všechny VLAN“). Nic dalšího nemění: porty zůstávají ve
VLAN 1 (PVID 1), správa jede dál tam, kde jela.

Uplink: STP root port, porty ke spravovaným zařízením a port, kde bridge vidí MAC výchozí brány
(ARP brány → bridge host). Správa přes VLAN rozhraní na bridgi → bridge je v té VLAN tagovaně.
"""

from __future__ import annotations

from typing import Any

from .portcfg import PortError, _members, _rows, _true
from .routeros import q


def _bridge(inv: dict) -> str | None:
    """Bridge s nejvíc porty (typicky jediný `bridge`)."""
    counts: dict[str, int] = {}
    for b in _rows(inv, "bridge_ports"):
        if not _true(b.get("dynamic")):
            counts[str(b.get("bridge"))] = counts.get(str(b.get("bridge")), 0) + 1
    return max(counts, key=lambda k: (counts[k], k == "bridge")) if counts else None


def gateway_ports(inv: dict, bridge: str) -> set[str]:
    """Port bridge, za kterým je výchozí brána (ARP → bridge host)."""
    gws = set()
    for r in _rows(inv, "routes_default"):
        if r.get("routing-table") not in (None, "", "main") or not _true(r.get("active", "true")):
            continue
        gw = str(r.get("gateway") or "").split("%")[0]
        if gw:
            gws.add(gw)
    for d in _rows(inv, "dhcp_client"):
        if d.get("gateway"):
            gws.add(str(d["gateway"]))
    macs = {str(a.get("mac-address") or "").upper() for a in _rows(inv, "arp") if str(a.get("address")) in gws}
    macs.discard("")
    return {str(h.get("on-interface")) for h in _rows(inv, "bridge_hosts")
            if str(h.get("mac-address") or "").upper() in macs and h.get("bridge") in (None, bridge)
            and h.get("on-interface")}


def mgmt_vlans(inv: dict, bridge: str) -> set[int]:
    """VLAN, ve kterých má zařízení vlastní adresu přes VLAN rozhraní na bridgi."""
    vl = {str(v.get("name")): v for v in _rows(inv, "vlans") if str(v.get("interface")) == bridge}
    used = {str(a.get("interface")) for a in _rows(inv, "addresses")} | \
        {str(d.get("interface")) for d in _rows(inv, "dhcp_client")}
    out = set()
    for name in used & set(vl):
        try:
            out.add(int(vl[name].get("vlan-id")))
        except (TypeError, ValueError):
            pass
    return out


def status(inv: dict, intent: dict | None) -> dict | None:
    """Pro UI: které sítě lokality na zařízení chybí a jestli je zapnuté vlan-filtering."""
    if not intent or not inv:
        return None
    bridge = _bridge(inv)
    if not bridge:
        return None
    have = set()
    for v in _rows(inv, "bridge_vlans"):
        if v.get("bridge") == bridge:
            for part in str(v.get("vlan-ids") or "").split(","):
                if "-" in part:
                    lo, _, hi = part.partition("-")
                    if lo.isdigit() and hi.isdigit():
                        have.update(range(int(lo), int(hi) + 1))
                elif part.isdigit():
                    have.add(int(part))
    vids = [int(n["vlan_id"]) for n in intent.get("networks") or []]
    br = next((b for b in _rows(inv, "bridges") if b.get("name") == bridge), {})
    return {"bridge": bridge, "site_vlans": vids, "missing": [v for v in vids if v != 1 and v not in have],
            "vlan_filtering": _true(br.get("vlan-filtering"))}


def plan(inv: dict, intent: dict, uplinks: set[str] | None = None) -> dict:
    if not intent or not intent.get("networks"):
        raise PortError("lokalita nemá sítě – nejdřív je založ v Lokalita → Sítě a DHCP")
    bridge = _bridge(inv)
    if not bridge:
        raise PortError("zařízení nemá bridge s porty")
    members = {str(b.get("interface")) for b in _rows(inv, "bridge_ports")
               if b.get("bridge") == bridge and not _true(b.get("dynamic"))}
    ups = sorted((set(uplinks or set()) | gateway_ports(inv, bridge)) & members)
    mvids = mgmt_vlans(inv, bridge)
    rows: dict[int, dict] = {}
    multi: set[int] = set()
    for v in _rows(inv, "bridge_vlans"):
        if v.get("bridge") != bridge or _true(v.get("dynamic")):
            continue
        ids = str(v.get("vlan-ids") or "")
        if ids.isdigit():
            rows[int(ids)] = v
        else:
            for part in ids.split(","):
                if "-" in part:
                    lo, _, hi = part.partition("-")
                    if lo.isdigit() and hi.isdigit():
                        multi.update(range(int(lo), int(hi) + 1))
                elif part.isdigit():
                    multi.add(int(part))
    br = q(bridge)
    apply, revert, changes, warn = [], [], [], []
    added = []
    for n in intent["networks"]:
        vid = int(n["vlan_id"])
        if vid == 1 and 1 not in mvids:
            continue  # VLAN 1 = netagovaná (PVID 1) – řádek netřeba
        if vid in multi:
            warn.append(f"VLAN {vid} je na zařízení v řádku s víc ID – nechávám beze změny")
            continue
        need = ups + (["bridge"] if vid in mvids else [])
        row = rows.get(vid)
        if row is None:
            apply.append(f'/interface bridge vlan add bridge={br} vlan-ids={vid} tagged={q(",".join(need))} comment="mtc"')
            revert.append(f'/interface bridge vlan remove [find bridge={br} vlan-ids={vid} comment="mtc"]')
            added.append(vid)
            changes.append({"field": "vlan", "before": None, "after": {"vlan": vid, "name": n.get("name"), "tagged": need}})
            continue
        old = _members(row.get("tagged"))
        new = old + [m for m in need if m not in old and m not in _members(row.get("untagged"))]
        if new != old:
            find = f"[find bridge={br} vlan-ids={vid} dynamic=no]"
            apply.append(f"/interface bridge vlan set {find} tagged={q(','.join(new))}")
            revert.append(f"/interface bridge vlan set {find} tagged={q(','.join(old))}")
            changes.append({"field": "vlan", "before": {"vlan": vid, "tagged": old}, "after": {"vlan": vid, "tagged": new}})
    bobj = next((b for b in _rows(inv, "bridges") if b.get("name") == bridge), {})
    if not _true(bobj.get("vlan-filtering")):
        apply.append(f"/interface bridge set [find name={br}] vlan-filtering=yes")
        revert.append(f"/interface bridge set [find name={br}] vlan-filtering=no")
        changes.append({"field": "vlan_filtering", "before": False, "after": True})
        warn.append("zapíná se vlan-filtering – porty zůstávají ve VLAN 1 (PVID 1), správa jede dál; "
                    "když se zařízení do 5 min neozve, změna se sama vrátí")
    if not ups:
        warn.append("uplink nepoznán (žádný root port, spravovaný soused ani brána za portem) – VLAN se jen "
                    "připraví bez členů; uplink otaguj profilem „Trunk – všechny VLAN“")
    if not changes:
        raise PortError("VLAN lokality už na zařízení jsou" + (f" ({'; '.join(warn)})" if warn else ""))
    parts = []
    if added:
        parts.append(f"VLAN {', '.join(map(str, added))}" + (f" tagovaně na {', '.join(ups)}" if ups else ""))
    if any(c["field"] == "vlan" and c["before"] for c in changes):
        parts.append("uplink doplněn do existujících VLAN")
    if any(c["field"] == "vlan_filtering" for c in changes):
        parts.append("vlan-filtering zapnuto")
    return {"changes": changes, "apply": "\n".join(apply) + "\n", "revert": "\n".join(reversed(revert)) + "\n",
            "warnings": warn, "summary": f"VLAN lokality na {bridge}: " + "; ".join(parts)}
