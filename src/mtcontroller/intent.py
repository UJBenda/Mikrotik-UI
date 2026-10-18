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
PORT_MODES = ("access", "trunk", "off")
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
    """Výjimky zařízení: {"ports": {port: {"mode": access|trunk|off, "vlan": vid}},
    "mgmt": {"mode": "dhcp"} | {"mode": "static", "address": "a.b.c.d/nn"}}."""
    if not isinstance(data, dict):
        raise IntentError("výjimky musí být objekt")
    out: dict[str, Any] = {"ports": {}, "mgmt": {"mode": "dhcp"}}
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
            raise IntentError(f"port {port}: režim musí být access, trunk nebo off")
        item: dict[str, Any] = {"mode": p["mode"]}
        if p["mode"] == "access" and p.get("vlan") not in (None, ""):
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
