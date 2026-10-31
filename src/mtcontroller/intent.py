"""Záměr lokality pro řízený režim (F5a): sítě, management a výchozí přístupová síť.

Uživatel zadává *co* chce (sítě a role zařízení), controller z toho počítá *jak*
(bridge, VLAN tabulky, role portů – switchcfg.py). Záměr se ukládá do
site_policy (module 'intent'); výjimky zařízení do device_intent.

Jen čisté funkce (validace a normalizace) – bez DB.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from . import regional

PURPOSES = {
    "mgmt": "management",
    "lan": "zaměstnanci / LAN",
    "hoste": "hosté",
    "iot": "IoT",
    "kamery": "kamery",
    "voip": "telefony",
    "jine": "jiné",
}
MAX_NETWORKS = 64
PORT_MODES = ("access", "trunk", "hybrid", "off")
PORT_NAME_RE = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")


class IntentError(ValueError):
    pass


def _vid(v: Any, what: str) -> int:
    try:
        n = int(str(v))
    except (TypeError, ValueError):
        raise IntentError(f"{what}: VLAN musí být číslo 1–4094") from None
    if not 1 <= n <= 4094:
        raise IntentError(f"{what}: VLAN musí být číslo 1–4094")
    return n


def _name(v: Any) -> str:
    s = str(v or "").strip()
    if not 1 <= len(s) <= 40 or any(ord(c) < 32 for c in s):
        raise IntentError("jméno sítě: 1–40 znaků bez řídicích znaků")
    return s


def _network(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise IntentError("síť musí být objekt")
    name = _name(raw.get("name"))
    vid = _vid(raw.get("vlan_id"), f"síť {name}")
    purpose = str(raw.get("purpose") or "lan")
    if purpose not in PURPOSES:
        raise IntentError(f"síť {name}: neznámý účel {purpose!r}")
    net: dict[str, Any] = {"name": name, "vlan_id": vid, "purpose": purpose,
                           "subnet": None, "gateway": None, "dns": []}
    if raw.get("subnet"):
        try:
            sn = ipaddress.ip_network(str(raw["subnet"]).strip(), strict=False)
        except ValueError:
            raise IntentError(f"síť {name}: neplatná podsíť") from None
        if sn.version != 4 or sn.prefixlen > 30:
            raise IntentError(f"síť {name}: podsíť musí být IPv4 s prefixem nejvýš /30")
        net["subnet"] = str(sn)
    if raw.get("gateway"):
        try:
            gw = ipaddress.ip_address(str(raw["gateway"]).strip())
        except ValueError:
            raise IntentError(f"síť {name}: neplatná brána") from None
        if net["subnet"] and gw not in ipaddress.ip_network(net["subnet"]):
            raise IntentError(f"síť {name}: brána {gw} není v podsíti {net['subnet']}")
        net["gateway"] = str(gw)
    dns = raw.get("dns") or []
    if isinstance(dns, str):
        dns = [x for x in re.split(r"[,\s]+", dns) if x]
    if not isinstance(dns, list) or len(dns) > 3:
        raise IntentError(f"síť {name}: nejvýš 3 DNS servery")
    for d in dns:
        try:
            net["dns"].append(str(ipaddress.IPv4Address(str(d).strip())))
        except ValueError:
            raise IntentError(f"síť {name}: neplatný DNS server {d!r}") from None
    return net


def validate(data: dict) -> dict:
    """Záměr lokality z UI → normalizovaný záměr (nebo IntentError)."""
    if not isinstance(data, dict):
        raise IntentError("záměr musí být objekt")
    raw_nets = data.get("networks")
    if not isinstance(raw_nets, list) or not raw_nets:
        raise IntentError("zadej aspoň jednu síť")
    if len(raw_nets) > MAX_NETWORKS:
        raise IntentError(f"nejvýš {MAX_NETWORKS} sítí")
    nets = [_network(n) for n in raw_nets]
    seen: dict[int, str] = {}
    names: set[str] = set()
    for n in nets:
        if n["vlan_id"] in seen:
            raise IntentError(f"VLAN {n['vlan_id']} mají dvě sítě ({seen[n['vlan_id']]}, {n['name']})")
        if n["name"].lower() in names:
            raise IntentError(f"dvě sítě se jmenují {n['name']}")
        seen[n["vlan_id"]] = n["name"]
        names.add(n["name"].lower())
    mgmt = [n for n in nets if n["purpose"] == "mgmt"]
    if len(mgmt) != 1:
        raise IntentError("právě jedna síť musí mít účel management")
    access = data.get("access_vlan")
    access_vid = _vid(access, "výchozí síť portů") if access not in (None, "") else None
    if access_vid is None:
        lan = next((n for n in nets if n["purpose"] == "lan"), mgmt[0])
        access_vid = lan["vlan_id"]
    if access_vid not in seen:
        raise IntentError("výchozí síť portů musí být jedna z definovaných sítí")
    nets.sort(key=lambda n: n["vlan_id"])
    return {
        "wifi": validate_wifi(data.get("wifi") or {}),
        "networks": nets,
        "mgmt_vlan": mgmt[0]["vlan_id"],
        # management bez tagu na trunkách: nové zařízení zapojené kamkoli hned dostane IP
        "mgmt_native": bool(data.get("mgmt_native", True)),
        "access_vlan": access_vid,
        # nový spravovaný soused na přístupovém portu → port se rozšíří na trunk (jen přidá VLAN)
        "auto_trunk": bool(data.get("auto_trunk", True)),
    }


def network(intent: dict, vid: int) -> dict | None:
    return next((n for n in intent.get("networks") or [] if n["vlan_id"] == vid), None)


def validate_device(data: dict, intent: dict | None, ports: set[str] | None = None) -> dict:
    """Výjimky zařízení: {"ports": {port: {"mode": access|trunk|hybrid|off, "vlan": vid, "tagged": [vid]}},
    hybrid = native VLAN `vlan` + vybrané tagované (vlastní profil portu);
    "mgmt": {"mode": "dhcp"} | {"mode": "static", "address": "a.b.c.d/nn"}}."""
    if not isinstance(data, dict):
        raise IntentError("výjimky musí být objekt")
    out: dict[str, Any] = {"ports": {}, "mgmt": {"mode": "dhcp"}, "role": None}
    if data.get("role") not in (None, "", "switch", "ap"):
        raise IntentError("role: switch nebo ap (AP + switch)")
    out["role"] = data.get("role") or None
    raw_ports = data.get("ports") or {}
    if not isinstance(raw_ports, dict) or len(raw_ports) > 128:
        raise IntentError("porty: neplatný seznam")
    vids = {n["vlan_id"] for n in (intent or {}).get("networks") or []}
    for port, p in raw_ports.items():
        if not PORT_NAME_RE.match(str(port)):
            raise IntentError(f"neplatné jméno portu {port!r}")
        if ports is not None and port not in ports:
            raise IntentError(f"port {port} na zařízení není")
        if not isinstance(p, dict) or p.get("mode") not in PORT_MODES:
            raise IntentError(f"port {port}: režim musí být access, trunk, hybrid nebo off")
        item: dict[str, Any] = {"mode": p["mode"]}
        if p["mode"] == "hybrid":
            raw = p.get("tagged") or []
            if not isinstance(raw, list) or len(raw) > 64:
                raise IntentError(f"port {port}: tagované VLAN – seznam (max. 64)")
            tagged = sorted({_vid(x, f"port {port}") for x in raw})
            if vids and set(tagged) - vids:
                raise IntentError(f"port {port}: VLAN {', '.join(map(str, sorted(set(tagged) - vids)))} není v sítích lokality")
            item["tagged"] = tagged
        if p["mode"] in ("access", "hybrid") and p.get("vlan") not in (None, ""):
            vid = _vid(p.get("vlan"), f"port {port}")
            if vids and vid not in vids:
                raise IntentError(f"port {port}: VLAN {vid} není v sítích lokality")
            item["vlan"] = vid
        out["ports"][port] = item
    m = data.get("mgmt") or {}
    if not isinstance(m, dict):
        raise IntentError("management: neplatné nastavení")
    if m.get("mode") == "static":
        try:
            ipi = ipaddress.IPv4Interface(str(m.get("address") or "").strip())
        except ValueError:
            raise IntentError("management: adresa ve tvaru 192.168.99.11/24") from None
        if ipi.network.prefixlen > 30 or ipi.ip == ipi.network.network_address:
            raise IntentError("management: neplatná adresa")
        mg = network(intent or {}, (intent or {}).get("mgmt_vlan") or 0) if intent else None
        if mg and mg.get("subnet") and ipi.ip not in ipaddress.ip_network(mg["subnet"]):
            raise IntentError(f"management: adresa není v podsíti {mg['subnet']}")
        if mg is not None and not mg.get("gateway"):
            raise IntentError("management: pro statickou adresu doplň bránu sítě management")
        out["mgmt"] = {"mode": "static", "address": str(ipi)}
    elif m.get("mode") not in (None, "", "dhcp"):
        raise IntentError("management: režim dhcp nebo static")
    return out


def from_observed(observed: list[dict]) -> dict:
    """Návrh záměru ze sítí zjištěných na zařízeních (dhcpguard.site_networks) –
    uživatel ho jen pojmenuje a potvrdí. Nic se tím nemění."""
    nets: list[dict] = []
    seen: set[int] = set()
    for o in observed:
        vid = o.get("vlan_id") or (1 if not o.get("tagged") else None)
        if not vid or vid in seen:
            continue
        seen.add(vid)
        nets.append({"name": str(o.get("comment") or o.get("name") or f"VLAN {vid}")[:40],
                     "vlan_id": vid, "purpose": "lan", "subnet": o.get("subnet"),
                     "gateway": o.get("gateway"), "dns": []})
    if nets:
        # management = netagovaná síť (nebo první); typicky tam dnes zařízení mají IP
        mg = next((n for n in nets if n["vlan_id"] == 1), nets[0])
        mg["purpose"] = "mgmt"
    return {"networks": nets, "mgmt_native": True, "auto_trunk": True,
            "access_vlan": next((n["vlan_id"] for n in nets if n["purpose"] == "lan"), nets[0]["vlan_id"] if nets else None)}


# --- Wi-Fi (F5b) -----------------------------------------------------------------------------------

COUNTRY_RE = re.compile(r"^[A-Za-z][A-Za-z .'-]{1,39}$")
SSID_BANDS = ("2.4", "5", "2.4,5")
MAX_SSIDS = 16         # na lokalitě (rozdělené do skupin AP)
MAX_SSIDS_PER_BAND = 4  # doporučení na rádio (beacony každého SSID zabírají vysílací čas) – nad tím varování
HARD_SSIDS_PER_RADIO = 8  # víc virtuálních AP rádia MikroTik spolehlivě neutáhnou
MAX_GROUPS = 32
MAX_PPSK = 1000        # na SSID (RouterOS: 10000 hesel celkem na zařízení)
GROUP_ID_RE = re.compile(r"^[a-z0-9-]{1,24}$")


def validate_wifi(raw: dict) -> dict:
    """Wi-Fi politika lokality: zařízení s CAPsMANem, země, DFS, max. šířka v 5 GHz."""
    if not isinstance(raw, dict):
        raise IntentError("Wi-Fi: neplatné nastavení")
    host = raw.get("capsman_device_id") or None
    if host is not None:
        host = str(host)
        if not re.fullmatch(r"[0-9a-f-]{36}", host):
            raise IntentError("Wi-Fi: neplatné zařízení pro CAPsMAN")
    country = str(raw.get("country") or regional.country()).strip()
    if not COUNTRY_RE.match(country):
        raise IntentError("Wi-Fi: neplatná země (např. Czech Republic)")
    try:
        width = int(raw.get("max_width") or 80)
    except (TypeError, ValueError):
        width = 0
    if width not in (20, 40, 80):
        raise IntentError("Wi-Fi: max. šířka 20, 40 nebo 80 MHz")
    groups = []
    seen: set[str] = set()
    raw_groups = raw.get("groups") or []
    if not isinstance(raw_groups, list) or len(raw_groups) > MAX_GROUPS:
        raise IntentError(f"Wi-Fi: nejvýš {MAX_GROUPS} skupin AP")
    for g in raw_groups:
        if not isinstance(g, dict):
            raise IntentError("Wi-Fi: skupina AP musí být objekt")
        name = str(g.get("name") or "").strip()
        if not 1 <= len(name) <= 40 or any(ord(c) < 32 for c in name):
            raise IntentError("Wi-Fi: jméno skupiny AP 1–40 znaků")
        gid = str(g.get("id") or "")
        if not GROUP_ID_RE.match(gid):
            import secrets

            gid = "g-" + secrets.token_hex(4)
        if gid in seen or name.lower() in {x["name"].lower() for x in groups}:
            raise IntentError(f"Wi-Fi: skupina {name} je dvakrát")
        seen.add(gid)
        groups.append({"id": gid, "name": name})
    tx = str(raw.get("tx_power") or "balanced")
    if tx not in ("balanced", "auto"):
        raise IntentError("Wi-Fi: výkon vysílání balanced | auto")
    rogue = str(raw.get("rogue_scan") or "off")
    if rogue not in ("off", "night"):
        raise IntentError("Wi-Fi: sken cizích AP off | night")
    return {"capsman_device_id": host, "country": country, "dfs": bool(raw.get("dfs", True)), "max_width": width,
            "groups": groups, "tx_power": tx, "steering": bool(raw.get("steering", True)), "rogue_scan": rogue}


def validate_ssid(raw: dict, vids: set[int], need_psk: bool, group_ids: set[str] | None = None) -> dict:
    """SSID z UI. Heslo (psk) je povinné u nového SSID; u úpravy prázdné = beze změny.
    groups = skupiny AP, kde se vysílá ([] = všechna AP); ppsk = více hesel s VLAN
    (None = beze změny, [] = vypnout; prázdné heslo položky = ponechat uložené podle jména)."""
    if not isinstance(raw, dict):
        raise IntentError("SSID musí být objekt")
    ssid = str(raw.get("ssid") or "")
    if not 1 <= len(ssid) <= 32 or not re.fullmatch(r"[\x20-\x7e]+", ssid) or ssid != ssid.strip():
        raise IntentError("SSID: 1–32 znaků bez diakritiky, bez mezer na začátku a konci")
    vid = _vid(raw.get("vlan_id"), f"SSID {ssid}")
    if vid not in vids:
        raise IntentError(f"SSID {ssid}: VLAN {vid} není v sítích lokality")
    bands = str(raw.get("bands") or "2.4,5")
    if bands not in SSID_BANDS:
        raise IntentError(f"SSID {ssid}: pásma 2.4, 5 nebo 2.4,5")
    psk = raw.get("psk")
    if psk in (None, ""):
        if need_psk:
            raise IntentError(f"SSID {ssid}: zadej heslo (8–63 znaků)")
        psk = None
    else:
        psk = str(psk)
        if not 8 <= len(psk) <= 63 or not re.fullmatch(r"[\x20-\x7e]+", psk):
            raise IntentError(f"SSID {ssid}: heslo 8–63 znaků bez diakritiky")
    groups = raw.get("groups") or []
    if not isinstance(groups, list) or any(not isinstance(g, str) for g in groups):
        raise IntentError(f"SSID {ssid}: neplatné skupiny AP")
    groups = sorted(set(groups))
    if group_ids is not None and set(groups) - group_ids:
        raise IntentError(f"SSID {ssid}: neznámá skupina AP")
    ppsk = raw.get("ppsk")
    if ppsk is not None:
        if not isinstance(ppsk, list) or len(ppsk) > MAX_PPSK:
            raise IntentError(f"SSID {ssid}: nejvýš {MAX_PPSK} hesel")
        norm, names, keys = [], set(), set()
        for e in ppsk:
            if not isinstance(e, dict):
                raise IntentError(f"SSID {ssid}: neplatná položka hesla")
            nm = str(e.get("name") or "").strip()
            if not 1 <= len(nm) <= 40 or not re.fullmatch(r"[\x20-\x7e]+", nm):
                raise IntentError(f"SSID {ssid}: jméno hesla 1–40 znaků bez diakritiky")
            if nm.lower() in names:
                raise IntentError(f"SSID {ssid}: heslo {nm} je dvakrát")
            names.add(nm.lower())
            v = _vid(e.get("vlan_id"), f"SSID {ssid} heslo {nm}")
            if v not in vids:
                raise IntentError(f"SSID {ssid} heslo {nm}: VLAN {v} není v sítích lokality")
            pk = e.get("psk")
            if pk not in (None, ""):
                pk = str(pk)
                if not 8 <= len(pk) <= 63 or not re.fullmatch(r"[\x20-\x7e]+", pk):
                    raise IntentError(f"SSID {ssid} heslo {nm}: 8–63 znaků bez diakritiky")
                if pk in keys or pk == psk:
                    raise IntentError(f"SSID {ssid}: dvě stejná hesla – klient by nešel přiřadit do VLAN")
                keys.add(pk)
            else:
                pk = None
            norm.append({"name": nm, "vlan_id": v, "psk": pk})
        ppsk = norm
    return {"ssid": ssid, "vlan_id": vid, "bands": bands, "isolation": bool(raw.get("isolation", False)),
            "ft": bool(raw.get("ft", True)), "enabled": bool(raw.get("enabled", True)), "psk": psk,
            "groups": groups, "ppsk": ppsk}
