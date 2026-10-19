"""Cílová konfigurace řízeného switche (F5a) ze záměru lokality.

Postup: inventura zařízení + záměr lokality (+ výjimky zařízení) → model
(role portů, VLAN tabulka, management) → buď celý bootstrap pro převzetí
načisto, nebo sladění (rozdíl proti inventuře) pro už řízené zařízení.

Model:
- jeden `bridge` se všemi ethernet/SFP porty, vlan-filtering, RSTP,
  ingress-filtering; DHCP snooping jen na čipech s HW podporou (trusted = uplink);
- management: L3 přímo na bridgi, bridge má pvid = VLAN managementu (je její
  netagovaný člen) – funguje stejně pro nativní i tagovanou mgmt na trunkách
  a i pro VLAN 1;
- trunk = uplink + porty se spravovaným/infrastrukturním sousedem: všechny sítě
  tagované, management nativně (pvid) nebo tagovaně podle záměru;
- přístupový port: pvid výchozí sítě (nebo výjimky), jen netagované rámce, loop protect;
- vypnutý port: ethernet disabled, mimo bridge.

Jména portů v modelu jsou výchozí jména (default-name): převzetí jde přes reset
konfigurace, po kterém mají porty zase výchozí jména.

Jen čisté funkce, bez DB. Vše do skriptů jde přes routeros.q().
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import unicodedata
from typing import Any

from . import intent as intentmod
from .routeros import q, q_source

BRIDGE = "bridge"
PHYS_RE = re.compile(r"^(ether|sfp|combo|qsfp)")
HW_SNOOPING = re.compile(r"^(CRS3|CRS5|CCR2116|CCR2216)", re.I)
FT_ACCESS = "admit-only-untagged-and-priority-tagged"
FT_TRUNK_TAGGED = "admit-only-vlan-tagged"
FT_ALL = "admit-all"
BACKUP_NAME = "mtc-takeover"
BOOT_FILE = "mtc-bootstrap.rsc"
TAKEOVER_MARK = "mtc-takeover"   # komentář revert skriptu během převzetí (značka pro check-in)
REVERT_TICKS = 12                # minut po startu bootstrapu do návratu ze zálohy
TAKEOVER_CONFIRM_MINUTES = 15    # termín na straně controlleru (reset + start + bootstrap)


def _rows(inv: dict, key: str) -> list[dict]:
    v = inv.get(key)
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def _obj(inv: dict, key: str) -> dict:
    v = inv.get(key)
    return v if isinstance(v, dict) else {}


def _true(v: Any) -> bool:
    return str(v).lower() in ("true", "yes")


def _members(v: Any) -> list[str]:
    return [x for x in str(v or "").split(",") if x]


def _nat_key(name: str) -> tuple:
    return tuple(int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name))


_PUNCT = str.maketrans({"–": "-", "—": "-", "„": '"', "“": '"', "”": '"', "‚": "'", "‘": "'", "’": "'", "…": "..."})


def ascii_text(v: Any, limit: int = 60) -> str:
    """Text pro RouterOS (komentář, identita): bez diakritiky, jen tisknutelné ASCII."""
    s = str(v or "").translate(_PUNCT)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^\x20-\x7e]", "", s).strip()
    return s[:limit]


# --- porty ------------------------------------------------------------------------------------

def physical_ports(inv: dict) -> list[dict]:
    """[{port: výchozí jméno, name: současné jméno, comment}] seřazené přirozeně."""
    comments = {str(i.get("name")): i.get("comment") for i in _rows(inv, "interfaces")}
    out = []
    for e in _rows(inv, "ethernet"):
        name = str(e.get("name") or "")
        default = str(e.get("default-name") or name)
        if not PHYS_RE.match(default):
            continue
        out.append({"port": default, "name": name, "comment": comments.get(name)})
    out.sort(key=lambda p: _nat_key(p["port"]))
    return out


def _to_default(inv: dict) -> dict[str, str]:
    return {p["name"]: p["port"] for p in physical_ports(inv)}


def _phys(inv: dict, iface: Any) -> str | None:
    names = _to_default(inv)
    for part in re.split(r"[,/]", str(iface or "")):
        if part in names:
            return names[part]
    return None


# --- klasifikace ------------------------------------------------------------------------------

_TUNNEL_TYPES = {"pppoe-out", "pppoe-in", "l2tp-out", "l2tp-in", "sstp-out", "sstp-in", "ovpn-out", "ovpn-in",
                 "pptp-out", "pptp-in", "wg", "wireguard", "gre", "eoip", "ipip", "vxlan", "lte", "ipsec"}


def classify(inv: dict, role: str = "switch") -> dict:
    """Co na zařízení je a co převzetí načisto odstraní.
    class: factory (defconf/prázdné), simple (jeden bridge bez VLAN), conforming
    (jeden bridge s vlan-filtering), custom (router, firewall, tunely… – jen po potvrzení)."""
    removed: list[str] = []
    custom: list[str] = []
    addrs = [a for a in _rows(inv, "addresses") if not _true(a.get("dynamic")) and not str(a.get("interface", "")).startswith("mtc")]
    for a in addrs:
        removed.append(f"IP adresa {a.get('address')} na {a.get('interface')}")
    fw = [r for r in _rows(inv, "fw_filter") if not _true(r.get("dynamic")) and not str(r.get("comment") or "").startswith("mtc")]
    nat = [r for r in _rows(inv, "fw_nat") if not _true(r.get("dynamic"))]
    defconf = lambda r: str(r.get("comment") or "").startswith("defconf")  # noqa: E731
    if fw:
        removed.append(f"firewall: {len(fw)} pravidel")
    if nat:
        removed.append(f"NAT: {len(nat)} pravidel")
    srv = [s for s in _rows(inv, "dhcp_servers") if not _true(s.get("disabled"))]
    for s in srv:
        removed.append(f"DHCP server {s.get('name')} na {s.get('interface')}")
    vlans = [v for v in _rows(inv, "vlans")]
    for v in vlans:
        removed.append(f"VLAN rozhraní {v.get('name')} (VLAN {v.get('vlan-id')})")
    bridges = [b for b in _rows(inv, "bridges")]
    if len(bridges) > 1:
        removed.append(f"{len(bridges)} bridge ({', '.join(str(b.get('name')) for b in bridges)}) → jeden")
    tunnels = [i for i in _rows(inv, "interfaces")
               if str(i.get("type")) in _TUNNEL_TYPES and not _true(i.get("dynamic")) and not str(i.get("name", "")).startswith("mtc")]
    for t in tunnels:
        removed.append(f"rozhraní {t.get('name')} ({t.get('type')})")
        custom.append(f"tunel/WAN {t.get('name')}")
    radios = [w for w in _rows(inv, "wifi") if not _true(w.get("disabled"))] + \
             [w for w in _rows(inv, "wireless") if not _true(w.get("disabled"))]
    if radios:
        removed.append(f"Wi-Fi: {len(radios)} rozhraní – " + (
            "rádia převezme CAPsMAN lokality" if role == "ap" else "vypne se (role switch)"))
    if any(not defconf(r) for r in fw):
        custom.append("vlastní pravidla firewallu")
    if any(not defconf(r) for r in nat):
        custom.append("vlastní NAT")
    if len(bridges) > 1:
        custom.append("víc bridgů")
    if any(not str(s.get("name") or "").startswith("defconf") for s in srv):
        custom.append("DHCP server")
    if any(str(v.get("name")) not in ("",) for v in vlans) and not (
            len(bridges) == 1 and _true(bridges[0].get("vlan-filtering"))):
        custom.append("VLAN rozhraní mimo vlan-filtering bridge")
    has_defconf = any(defconf(r) for r in fw + nat) or any(str(s.get("name") or "").startswith("defconf") for s in srv)
    defconf_like = all(defconf(r) for r in fw + nat) and all(
        str(a.get("address")) == "192.168.88.1/24" for a in addrs) and not vlans and len(bridges) <= 1 and (
        has_defconf or (not bridges and not addrs))
    if custom:
        cls = "custom"
    elif defconf_like:
        cls = "factory"
    elif len(bridges) == 1 and _true(bridges[0].get("vlan-filtering")):
        cls = "conforming"
    else:
        cls = "simple"
    return {"class": cls, "removed": removed, "custom": custom}


# --- cesta ke správě (uplink) -------------------------------------------------------------------

def mgmt_path(inv: dict) -> dict:
    """Kudy zařízení dnes dosáhne brány (a tím controlleru): L3 rozhraní, adresa,
    brána, fyzický uplink a zda je management na uplinku tagovaný.

    Uplink: port, na kterém bridge host tabulka vidí MAC brány (z ARP); když L3
    leží přímo na fyzickém portu, je to on; jinak root port RSTP."""
    out: dict[str, Any] = {"l3": None, "address": None, "gateway": None, "mode": None, "uplink": None,
                           "tagged_vid": None, "how": None, "dns": []}
    dhcp = next((c for c in _rows(inv, "dhcp_client") if not _true(c.get("disabled"))
                 and str(c.get("status") or "") == "bound"), None)
    route = next((r for r in _rows(inv, "routes_default") if _true(r.get("active")) and
                  str(r.get("routing-table") or "main") == "main"), None)
    gw = None
    if route:
        g = str(route.get("immediate-gw") or route.get("gateway") or "")
        gw, _, iface = g.partition("%")
        out["l3"] = iface or None
    if dhcp:
        out["mode"] = "dhcp"
        out["l3"] = out["l3"] or str(dhcp.get("interface"))
        gw = gw or str(dhcp.get("gateway") or "")
        out["address"] = dhcp.get("address")
    try:
        gw_ip = ipaddress.ip_address(gw) if gw else None
    except ValueError:
        gw_ip = None
    out["gateway"] = str(gw_ip) if gw_ip else None
    if not out["mode"] and gw_ip:
        for a in _rows(inv, "addresses"):
            try:
                ipi = ipaddress.ip_interface(str(a.get("address")))
            except ValueError:
                continue
            if gw_ip in ipi.network and not _true(a.get("disabled")):
                out["mode"] = "static"
                out["address"] = str(ipi)
                out["l3"] = out["l3"] or str(a.get("interface"))
                break
    dns = _obj(inv, "dns").get("servers")
    out["dns"] = [d for d in str(dns or "").split(",") if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", d)]
    l3 = out["l3"]
    if not l3:
        return out
    vlans = {str(v.get("name")): v for v in _rows(inv, "vlans")}
    bridges = {str(b.get("name")): b for b in _rows(inv, "bridges")}
    names = _to_default(inv)
    base = l3
    if l3 in vlans:
        vid = str(vlans[l3].get("vlan-id") or "")
        out["tagged_vid"] = int(vid) if vid.isdigit() else None
        base = str(vlans[l3].get("interface") or "")
    if base in names:
        out["uplink"], out["how"] = names[base], "L3 přímo na portu"
        return out
    if base in bridges:
        # netagovaná mgmt na bridgi s vlan-filtering = VLAN podle pvid bridge (na uplinku nativně)
        mac = next((str(a.get("mac-address")).upper() for a in _rows(inv, "arp")
                    if str(a.get("address")) == out["gateway"] and a.get("mac-address")), None)
        if mac:
            for h in _rows(inv, "bridge_hosts"):
                if str(h.get("mac-address") or "").upper() == mac and not _true(h.get("local")):
                    p = _phys(inv, h.get("on-interface"))
                    if p:
                        out["uplink"], out["how"] = p, "MAC brány v tabulce bridge"
                        return out
        for s in _rows(inv, "stp_ports"):
            if str(s.get("role")) == "root-port":
                p = _phys(inv, s.get("interface"))
                if p:
                    out["uplink"], out["how"] = p, "root port RSTP"
                    return out
    return out


# --- role portů a model -----------------------------------------------------------------------------

def _infra_neighbor(n: dict) -> bool:
    """Soused, za kterým je další síťové zařízení (switch, AP): trunk.
    Telefony hlásí schopnost bridge kvůli průchozímu portu pro PC – ty ne."""
    platform = str(n.get("platform") or "")
    caps = str(n.get("system-caps-enabled") or n.get("system-caps") or "").lower()
    if "telephone" in caps or "station" in caps:
        return False
    return "mikrotik" in platform.lower() or any(c in caps for c in ("bridge", "wlan-ap", "wlan"))


def build(inv: dict, intent: dict, dev_intent: dict | None = None, *, model_name: str = "",
          identity: str = "", ntp: dict | None = None, role: str = "switch", cap_manager: str | None = None) -> dict:
    """Model cílové konfigurace. `blockers` = převzetí/sladění nelze provést.
    role "ap" = AP + switch: rádia řídí CAPsMAN lokality (`cap_manager` = jeho identita)."""
    from .wificfg import supports_wifi

    dev_intent = dev_intent or {}
    blockers: list[str] = []
    warnings: list[str] = []
    cap = None
    if role == "ap":
        from .packages import wifi_status

        sw = supports_wifi(inv)
        ws = wifi_status(inv)
        if sw is None and not ws["installed"]:
            blockers.append("zařízení nemá Wi-Fi rádia – role AP nedává smysl")
        elif not ws["ok"]:
            blockers.append(f"Wi-Fi ovladač: {ws['problem']}" +
                            (f" – v detailu zařízení „Vyměnit Wi-Fi ovladač“ na {ws['fix']['install']}" if ws["fix"] else ""))
        elif sw is False:
            blockers.append("rádia mají jen starý balík wireless – řízený režim umí AP s balíkem wifi "
                            "(wifi-qcom, wifi-qcom-ac); nech roli switch")
        if not cap_manager:
            blockers.append("lokalita nemá CAPsMAN – vyber ho v záměru lokality (Wi-Fi)")
        cap = {"manager": ascii_text(cap_manager, 60) if cap_manager else ""}
    ports = physical_ports(inv)
    if not ports:
        blockers.append("inventura neobsahuje žádné ethernet/SFP porty")
    path = mgmt_path(inv)
    uplink = path["uplink"]
    if not uplink:
        blockers.append("nepodařilo se zjistit uplink (port k bráně) – zařízení musí mít výchozí routu "
                        "a bránu v ARP; zkus to po další inventuře")
    mg_vid = int(intent["mgmt_vlan"])
    native = bool(intent.get("mgmt_native", True))
    if path["l3"] and uplink:
        if native and path["tagged_vid"] is not None:
            blockers.append(f"zařízení dnes dosahuje brány přes VLAN {path['tagged_vid']} tagovaně, záměr chce "
                            "management bez tagu – po převzetí by se neozvalo")
        if not native and path["tagged_vid"] != mg_vid:
            now = f"VLAN {path['tagged_vid']}" if path["tagged_vid"] else "bez tagu"
            blockers.append(f"zařízení dnes dosahuje brány {now}, záměr chce management tagovaný ve VLAN {mg_vid} "
                            "– po převzetí by se neozvalo")
    vids = [n["vlan_id"] for n in intent["networks"]]
    over = (dev_intent.get("ports") or {})
    names = _to_default(inv)
    # výjimky se ukládají pod současnými jmény portů; model pracuje s výchozími
    over = {names.get(k, k): v for k, v in over.items()}
    neighbors: dict[str, str] = {}
    for n in _rows(inv, "neighbors"):
        p = _phys(inv, n.get("interface"))
        if p and _infra_neighbor(n):
            neighbors.setdefault(p, str(n.get("identity") or n.get("board") or n.get("mac-address") or "soused"))
    out_ports: list[dict] = []
    for p in ports:
        port = p["port"]
        o = over.get(port) or {}
        if port == uplink:
            mode, reason = "trunk", f"uplink ({path['how']})"
            if o and o.get("mode") != "trunk":
                blockers.append(f"{port} je uplink – nesmí být {o.get('mode')}")
        elif o:
            mode, reason = o["mode"], "výjimka"
        elif port in neighbors:
            mode, reason = "trunk", f"soused {neighbors[port]}"
        else:
            mode, reason = "access", "výchozí"
        item: dict[str, Any] = {"port": port, "name": p["name"], "mode": mode, "reason": reason,
                                "comment": ascii_text(p["comment"] or (p["name"] if p["name"] != port else ""), 60) or None}
        if mode == "access":
            vid = int(o.get("vlan") or intent["access_vlan"]) if mode == o.get("mode") else int(intent["access_vlan"])
            if vid not in vids:
                blockers.append(f"{port}: VLAN {vid} není v sítích lokality")
            item.update(pvid=vid, frame_types=FT_ACCESS, loop=True)
        elif mode == "trunk":
            item.update(pvid=mg_vid if native else 1, frame_types=FT_ALL if native else FT_TRUNK_TAGGED, loop=False)
        else:
            item.update(pvid=None, frame_types=None, loop=False)
        item["trusted"] = port == uplink
        out_ports.append(item)
    vlans = []
    for n in intent["networks"]:
        vid = n["vlan_id"]
        tagged, untagged = [], []
        if vid == mg_vid:
            untagged.append(BRIDGE)
        for it in out_ports:
            if it["mode"] == "trunk":
                (untagged if native and vid == mg_vid else tagged).append(it["port"])
            elif it["mode"] == "access" and it["pvid"] == vid:
                untagged.append(it["port"])
        vlans.append({"vid": vid, "name": n["name"], "tagged": tagged, "untagged": untagged})
    mnet = intentmod.network(intent, mg_vid) or {}
    dm = (dev_intent.get("mgmt") or {})
    if dm.get("mode") == "static":
        mgmt = {"mode": "static", "address": dm["address"], "gateway": mnet.get("gateway"), "dns": mnet.get("dns") or []}
    elif dm.get("mode") == "dhcp" or path["mode"] != "static":
        mgmt = {"mode": "dhcp"}
    else:
        # dnes statická adresa a výjimka nezadaná → zachovat (jinak by bez DHCP v mgmt síti zmizelo)
        mgmt = {"mode": "static", "address": path["address"], "gateway": mnet.get("gateway") or path["gateway"],
                "dns": mnet.get("dns") or path["dns"]}
        warnings.append(f"management zůstane staticky {path['address']} (tak je to dnes)")
    if mgmt["mode"] == "static" and not mgmt.get("gateway"):
        blockers.append("pro statickou adresu managementu chybí brána (doplň ji v síti management)")
    if mgmt["mode"] == "static" and not mgmt.get("dns"):
        warnings.append("statický management bez DNS – doplň DNS v síti management (jinak se nespojí s controllerem)")
    snooping = bool(HW_SNOOPING.match(model_name or ""))
    if not snooping:
        warnings.append("DHCP snooping vynechán: čip nemá HW podporu (běžel by na CPU)")
    if path["mode"] == "dhcp" and mgmt["mode"] == "dhcp":
        warnings.append("management přes DHCP – doporučuji rezervaci adresy na DHCP serveru")
    access_like = [p["port"] for p in out_ports if p["mode"] == "access"]
    if uplink and not access_like and len(out_ports) > 1:
        warnings.append("všechny porty jsou trunky")
    ntp = ntp or {}
    return {
        "bridge": BRIDGE, "ports": out_ports, "vlans": vlans, "uplink": uplink, "role": role, "cap": cap,
        "mgmt": {"vid": mg_vid, "native": native, **mgmt}, "path": path,
        "snooping": snooping, "identity": ascii_text(identity, 60) or "mtc-switch",
        "timezone": ntp.get("timezone") or "Europe/Prague",
        "ntp": [h for h in (ntp.get("upstream") or ["tik.cesnet.cz", "tak.cesnet.cz"]) if re.fullmatch(r"[A-Za-z0-9.-]{1,100}", h)],
        "blockers": blockers, "warnings": warnings,
    }


def model_sha(model: dict) -> str:
    keep = {k: model.get(k) for k in ("ports", "vlans", "mgmt", "snooping", "identity", "cap")}
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode()).hexdigest()


# --- bootstrap (převzetí načisto) ------------------------------------------------------------------

def _port_line(it: dict) -> str:
    c = f" comment={q('mtc ' + it['comment'])}" if it.get("comment") else ' comment="mtc"'
    edge = " edge=yes" if it["mode"] == "access" else ""
    trusted = " trusted=yes" if it.get("trusted") else ""
    return (f"/interface bridge port add bridge={BRIDGE} interface={q(it['port'])} pvid={int(it['pvid'])} "
            f"frame-types={it['frame_types']} ingress-filtering=yes{edge}{trusted}{c}")


def config_body(model: dict) -> str:
    """Bridge, porty, VLAN tabulka, management, systém. Předpokládá čistou konfiguraci."""
    m = model["mgmt"]
    lines = [
        f"/interface bridge add name={BRIDGE} vlan-filtering=no protocol-mode=rstp pvid={int(m['vid'])} "
        f"frame-types={FT_ACCESS} ingress-filtering=yes igmp-snooping=no comment=\"mtc\"",
    ]
    for it in model["ports"]:
        if it["mode"] == "off":
            lines.append(f"/interface ethernet set [find default-name={q(it['port'])}] disabled=yes")
            continue
        lines.append(_port_line(it))
        # loop protect neumí každý port – chyba nesmí shodit celé převzetí
        lines.append(f":do {{ /interface ethernet set [find default-name={q(it['port'])}] loop-protect="
                     f"{'on' if it['loop'] else 'off'} }} on-error={{}}")
    for v in model["vlans"]:
        lines.append(f"/interface bridge vlan add bridge={BRIDGE} vlan-ids={int(v['vid'])} "
                     f"tagged={q(','.join(v['tagged']))} untagged={q(','.join(v['untagged']))} "
                     f"comment={q('mtc:v' + str(v['vid']))}")
    if model.get("snooping"):
        lines.append(f":do {{ /interface bridge set {BRIDGE} dhcp-snooping=yes }} on-error={{}}")
    # filtrování zapnout až s hotovou VLAN tabulkou
    lines.append(f"/interface bridge set {BRIDGE} vlan-filtering=yes")
    if m["mode"] == "static":
        lines.append(f"/ip address add address={q(m['address'])} interface={BRIDGE} comment=\"mtc\"")
        lines.append(f"/ip route add dst-address=0.0.0.0/0 gateway={q(m['gateway'])} comment=\"mtc mgmt\"")
        if m.get("dns"):
            lines.append(f"/ip dns set servers={q(','.join(m['dns']))}")
    else:
        lines.append(f"/ip dhcp-client add interface={BRIDGE} use-peer-dns=yes use-peer-ntp=no "
                     f"add-default-route=yes comment=\"mtc\" disabled=no")
    if model.get("cap"):
        from .wificfg import cap_body

        lines.append(cap_body(model["cap"]["manager"]).rstrip())
    lines += [
        f"/system identity set name={q(model['identity'])}",
        f"/system clock set time-zone-autodetect=no time-zone-name={q(model['timezone'])}",
        f"/system ntp client set enabled=yes servers={q(','.join(model['ntp']))}",
        "/ip dns set allow-remote-requests=no",
        # zbytečné služby vypnout; www-ssl (REST pro controller) nastaví provisioning
        ':foreach s in={"telnet";"ftp";"www";"api"} do={ :do { /ip service set [find name=$s] disabled=yes } on-error={} }',
        "/tool mac-server set allowed-interface-list=none",
    ]
    return "\n".join(lines) + "\n"


def _revert_source(reverted_url: str, auth_header: str, check_certificate: str, backup_password: str) -> str:
    """Návrat ze zálohy: nahlásit controlleru, pak `/system backup load` (restart do původního stavu)."""
    return (f":do {{ :local a [/tool fetch url={q(reverted_url)} http-method=post "
            f"http-header-field={q(auth_header)} output=none check-certificate={check_certificate} as-value] }} on-error={{}}\n"
            ':log warning "mtc: prevzeti nepotvrzeno controllerem - obnovuji puvodni konfiguraci ze zalohy"\n'
            ':local p ""\n'
            ':if ([:len [/file find name="flash" type="directory"]] > 0) do={ :set p "flash/" }\n'
            f"/system backup load name=($p . {q(BACKUP_NAME + '.backup')}) password={q(backup_password)}\n")


def bootstrap(model: dict, adopt_body: str, checkin_install: str, *, reverted_url: str, auth_header: str,
              check_certificate: str, backup_password: str) -> str:
    """Skript pro `run-after-reset`: počká na porty, nastaví návrat ze zálohy,
    nasadí konfiguraci a znovu založí správu controllerem (tunel, účet, check-in).

    Návrat: tik každou minutu; po REVERT_TICKS bez potvrzení (CONFIRM_BODY smaže
    scheduler) se obnoví záloha pořízená těsně před resetem."""
    n = len(model["ports"])
    tick = (":global mtcConfirmTicks\n"
            ':if ([:typeof $mtcConfirmTicks] != "num") do={ :set mtcConfirmTicks 0 }\n'
            ":set mtcConfirmTicks ($mtcConfirmTicks + 1)\n"
            f":if ($mtcConfirmTicks >= {REVERT_TICKS}) do={{\n"
            "  :set mtcConfirmTicks 0\n"
            '  /system scheduler remove [find name="mtc-confirm"]\n'
            '  /system script run [find name="mtc-revert"]\n'
            "}\n")
    revert = _revert_source(reverted_url, auth_header, check_certificate, backup_password)
    from .routeros import SCRIPT_POLICY

    return f"""# MikroTik controller - prevzeti zarizeni (bootstrap po resetu)
{{
:local i 0
:while ($i < 90 && [:len [/interface ethernet find]] < {n}) do={{ :delay 1s; :set i ($i + 1) }}
:delay 5s
:log warning "mtc: bootstrap prevzeti zacina"
# --- navrat ze zalohy, kdyz controller prevzeti nepotvrdi ---
/system script add name="mtc-revert" policy={SCRIPT_POLICY} comment="{TAKEOVER_MARK}" source={q_source(revert)}
/system script add name="mtc-confirm-tick" policy={SCRIPT_POLICY} comment="mtc" source={q_source(tick)}
:global mtcConfirmTicks
:set mtcConfirmTicks 0
# explicitni start (RouterOS 7.24-7.24.4 nespousti scheduler s vychozim startem; startup by cekal na dalsi boot)
/system scheduler add name="mtc-confirm" interval=1m start-date=[/system clock get date] start-time=00:00:00 policy={SCRIPT_POLICY} comment="mtc" on-event="/system script run mtc-confirm-tick"
:local ok true
:do {{
{config_body(model).rstrip()}
}} on-error={{ :set ok false; :log error "mtc: bootstrap - chyba konfigurace" }}
# --- sprava controllerem (tunel, ucet, certifikat, firewall, check-in) ---
:if ($ok) do={{
:do {{
{adopt_body.rstrip()}
{checkin_install.rstrip()}
}} on-error={{ :set ok false; :log error "mtc: bootstrap - chyba pri obnove spravy" }}
}}
# polovicata konfigurace se nesmi potvrdit: hned zpet do zalohy
:if (!$ok) do={{ /system script run [find name="mtc-revert"] }}
:local p ""
:if ([:len [/file find name="flash" type="directory"]] > 0) do={{ :set p "flash/" }}
/file remove [find name=($p . "{BOOT_FILE}")]
/file remove [find name=($p . "mtc-cmd.rsc")]
:log warning "mtc: bootstrap hotov, ceka na potvrzeni controllerem"
}}
"""


def takeover_body(bootstrap_url: str, auth_header: str, check_certificate: str, backup_password: str) -> str:
    """Příkaz před resetem: záloha, stažení bootstrapu, kontrola. Reset je až v takeover_tail
    (po odeslání ack)."""
    return f""":local p ""
:if ([:len [/file find name="flash" type="directory"]] > 0) do={{ :set p "flash/" }}
/file remove [find name=($p . "{BOOT_FILE}")]
/file remove [find name=($p . "{BACKUP_NAME}.backup")]
/system backup save name=($p . "{BACKUP_NAME}") password={q(backup_password)}
:delay 3s
:if ([:len [/file find name=($p . "{BACKUP_NAME}.backup")]] = 0) do={{ :error "mtc: zaloha pred prevzetim se nevytvorila" }}
/tool fetch url={q(bootstrap_url)} http-method=post http-header-field={q(auth_header)} dst-path=($p . "{BOOT_FILE}") check-certificate={check_certificate}
:delay 2s
:if ([:len [/file find name=($p . "{BOOT_FILE}")]] = 0) do={{ :error "mtc: bootstrap se nestahl" }}
:global mtcBoot ($p . "{BOOT_FILE}")
:log warning "mtc: zaloha a bootstrap pripraveny, nasleduje reset konfigurace"
"""


TAKEOVER_TAIL = """:global mtcBoot
:if ([:len $mtcBoot] > 0) do={
  :delay 2s
  /system reset-configuration keep-users=yes no-defaults=yes skip-backup=yes run-after-reset=$mtcBoot
}
"""

# potvrzení převzetí: jen když na zařízení běží bootstrap (značka) – po návratu ze zálohy
# se zařízení ozve ve staré konfiguraci a potvrzení musí selhat
TAKEOVER_CONFIRM = (f':if ([:len [/system script find name="mtc-revert" comment="{TAKEOVER_MARK}"]] = 0) do={{ '
                    ':error "mtc: prevzeti neni aktivni" }\n'
                    ':global mtcConfirmTicks\n:set mtcConfirmTicks 0\n'
                    '/system scheduler remove [find name="mtc-confirm"]\n'
                    '/system script remove [find name="mtc-confirm-tick"]\n'
                    '/system script remove [find name="mtc-revert"]\n'
                    ':local p ""\n'
                    ':if ([:len [/file find name="flash" type="directory"]] > 0) do={ :set p "flash/" }\n'
                    f'/file remove [find name=($p . "{BACKUP_NAME}.backup")]\n'
                    ':log info "mtc: prevzeti potvrzeno controllerem"\n')


# --- sladění řízeného zařízení se záměrem -------------------------------------------------------------

def _vlan_entries(inv: dict) -> dict[int, dict]:
    out = {}
    for v in _rows(inv, "bridge_vlans"):
        if _true(v.get("dynamic")) or str(v.get("bridge")) != BRIDGE:
            continue
        ids = str(v.get("vlan-ids") or "")
        if ids.isdigit():
            out[int(ids)] = v
    return out


def reconcile(inv: dict, model: dict) -> dict:
    """Rozdíl modelu proti inventuře řízeného zařízení → skript a návrat.

    Mění jen to, co controller při převzetí vytvořil: porty bridge `bridge`,
    položky VLAN `mtc:v<vid>`, loop protect a vypnutí portů. `additive` = jen
    přibývají tagované VLAN (bezpečné i bez schválení)."""
    names = {p["port"]: p["name"] for p in physical_ports(inv)}
    bports = {_phys(inv, b.get("interface")): b for b in _rows(inv, "bridge_ports")
              if str(b.get("bridge")) == BRIDGE and not _true(b.get("dynamic"))}
    eth = {str(e.get("default-name") or e.get("name")): e for e in _rows(inv, "ethernet")}
    cur_vlans = _vlan_entries(inv)
    to_cur = lambda ms: {names.get(x, x) for x in ms}  # noqa: E731
    apply: list[str] = []
    revert: list[str] = []
    changes: list[str] = []
    additive = True
    # VLAN tabulka (nejdřív přidat/rozšířit, porty potom)
    phys_names = set(names.values()) | {BRIDGE}
    for v in model["vlans"]:
        vid = v["vid"]
        want_t, want_u = set(v["tagged"]), set(v["untagged"])
        cur = cur_vlans.get(vid)
        # členy, které nejsou fyzické porty (rádia – drží je skript mtc-wifi-vlan na AP), ponechat
        extra_t = [x for x in _members((cur or {}).get("tagged")) if x not in phys_names]
        tag = q(",".join([names.get(x, x) for x in v["tagged"]] + extra_t))
        untag = q(",".join(names.get(x, x) if x != BRIDGE else x for x in v["untagged"]))
        if not cur:
            apply.append(f"/interface bridge vlan add bridge={BRIDGE} vlan-ids={vid} tagged={tag} untagged={untag} "
                         f"comment={q('mtc:v' + str(vid))}")
            revert.append(f"/interface bridge vlan remove [find bridge={BRIDGE} vlan-ids={vid} dynamic=no]")
            changes.append(f"nová VLAN {vid} ({ascii_text(v['name'], 30)})")
            if want_u - {BRIDGE}:
                additive = False
            continue
        ct = {x for x in _members(cur.get("tagged")) if x in phys_names}
        cu = {x for x in _members(cur.get("untagged")) if x in phys_names}
        if to_cur(want_t) != ct or to_cur(want_u) != cu:
            apply.append(f"/interface bridge vlan set [find bridge={BRIDGE} vlan-ids={vid} dynamic=no] tagged={tag} untagged={untag}")
            revert.append(f"/interface bridge vlan set [find bridge={BRIDGE} vlan-ids={vid} dynamic=no] "
                          f"tagged={q(','.join(sorted(ct) + extra_t))} untagged={q(','.join(sorted(cu)))}")
            added = to_cur(want_t) - ct
            gone = (ct - to_cur(want_t)) | (cu ^ to_cur(want_u))
            changes.append(f"VLAN {vid}: " + ", ".join(
                ([f"+tag {','.join(sorted(added))}"] if added else []) + ([f"změna {','.join(sorted(gone))}"] if gone else [])))
            if gone:
                additive = False
    # bridge: VLAN managementu (bridge je její netagovaný člen) – před porty, ať netagovaný provoz nezmizí
    br = next((b for b in _rows(inv, "bridges") if str(b.get("name")) == BRIDGE), None)
    want_pvid = str(model["mgmt"]["vid"])
    if br is not None and str(br.get("pvid") or "1") != want_pvid:
        apply.append(f"/interface bridge set {BRIDGE} pvid={want_pvid}")
        revert.append(f"/interface bridge set {BRIDGE} pvid={br.get('pvid') or 1}")
        changes.append(f"management bridge: VLAN {br.get('pvid') or 1} → {want_pvid}")
        additive = False
    # porty
    for it in model["ports"]:
        port, name = it["port"], names.get(it["port"], it["port"])
        cur = bports.get(port)
        e = eth.get(port) or {}
        if it["mode"] == "off":
            if cur:
                apply.append(f"/interface bridge port remove [find bridge={BRIDGE} interface={q(name)}]")
                revert.append(_port_line({**_cur_item(cur), "port": name}))
                changes.append(f"{name}: vypnout")
                additive = False
            if not _true(e.get("disabled")):
                apply.append(f"/interface ethernet set [find default-name={q(port)}] disabled=yes")
                revert.append(f"/interface ethernet set [find default-name={q(port)}] disabled=no")
            continue
        if not cur:
            apply.append(_port_line({**it, "port": name}))
            revert.append(f"/interface bridge port remove [find bridge={BRIDGE} interface={q(name)}]")
            changes.append(f"{name}: do bridge ({it['mode']})")
            additive = False
            if _true(e.get("disabled")):
                apply.insert(0, f"/interface ethernet set [find default-name={q(port)}] disabled=no")
                revert.append(f"/interface ethernet set [find default-name={q(port)}] disabled=yes")
            continue
        diff = []
        if str(cur.get("pvid") or "1") != str(it["pvid"]):
            diff.append(("pvid", str(cur.get("pvid") or "1"), str(it["pvid"])))
        if str(cur.get("frame-types") or FT_ALL) != it["frame_types"]:
            diff.append(("frame-types", str(cur.get("frame-types") or FT_ALL), it["frame_types"]))
        if _true(cur.get("trusted")) != bool(it.get("trusted")):
            diff.append(("trusted", "yes" if _true(cur.get("trusted")) else "no", "yes" if it.get("trusted") else "no"))
        if diff:
            apply.append(f"/interface bridge port set [find bridge={BRIDGE} interface={q(name)}] "
                         + " ".join(f"{k}={new}" for k, _old, new in diff))
            revert.append(f"/interface bridge port set [find bridge={BRIDGE} interface={q(name)}] "
                          + " ".join(f"{k}={old}" for k, old, _new in diff))
            changes.append(f"{name}: " + ", ".join(f"{k} {old} → {new}" for k, old, new in diff))
            # trunk z přístupového portu se zachovanou pvid je jen rozšíření
            if any(k != "frame-types" for k, _o, _n in diff) or it["frame_types"] != FT_ALL:
                additive = False
        want_loop = "on" if it["loop"] else "off"
        cur_loop = str(e.get("loop-protect") or "default")
        if e and cur_loop != want_loop and not (cur_loop == "default" and want_loop == "off"):
            apply.append(f"/interface ethernet set [find default-name={q(port)}] loop-protect={want_loop}")
            revert.append(f"/interface ethernet set [find default-name={q(port)}] loop-protect={cur_loop}")
    # nepotřebné VLAN až nakonec (porty už mají nové pvid)
    keep = {v["vid"] for v in model["vlans"]}
    for vid, cur in sorted(cur_vlans.items()):
        if vid not in keep:
            apply.append(f"/interface bridge vlan remove [find bridge={BRIDGE} vlan-ids={vid} dynamic=no]")
            revert.append(f"/interface bridge vlan add bridge={BRIDGE} vlan-ids={vid} tagged={q(str(cur.get('tagged') or ''))} "
                          f"untagged={q(str(cur.get('untagged') or ''))} comment={q('mtc:v' + str(vid))}")
            changes.append(f"odebrat VLAN {vid}")
            additive = False
    # adresa managementu: DHCP ↔ statická (s návratem, přeruší-li to správu)
    a, r, c = _mgmt_diff(inv, model["mgmt"])
    if a:
        apply += a
        revert += r
        changes += c
        additive = False
    # CAP (AP + switch): řídí ho CAPsMAN lokality
    capcur = inv.get("wifi_cap") if isinstance(inv.get("wifi_cap"), dict) else None
    if capcur is not None:
        want = model.get("cap")
        on = _true(capcur.get("enabled"))
        cur_names = str(capcur.get("caps-man-names") or "")
        from .wificfg import cap_body, radios_need_capsman

        if want and (not on or cur_names != want["manager"] or str(capcur.get("discovery-interfaces") or "") != BRIDGE
                     or not _true(capcur.get("lock-to-caps-man")) or radios_need_capsman(inv)):

            apply.append(cap_body(want["manager"]).rstrip())
            revert.append(f"/interface wifi cap set enabled={'yes' if on else 'no'} caps-man-names={q(cur_names)} "
                          f"discovery-interfaces={q(str(capcur.get('discovery-interfaces') or ''))} "
                          f"lock-to-caps-man={'yes' if _true(capcur.get('lock-to-caps-man')) else 'no'}")
            changes.append(f"CAP → CAPsMAN {want['manager']}")
            additive = False
        elif not want and on:
            apply.append("/interface wifi cap set enabled=no")
            revert.append("/interface wifi cap set enabled=yes")
            changes.append("CAP vypnout (role switch)")
            additive = False
    # návrat v opačném pořadí
    revert.reverse()
    return {"apply": "\n".join(apply) + ("\n" if apply else ""), "revert": "\n".join(revert) + ("\n" if revert else ""),
            "changes": changes, "additive": additive and bool(changes),
            "summary": "; ".join(changes[:6]) + (f" (+{len(changes) - 6})" if len(changes) > 6 else "")}


def _mgmt_diff(inv: dict, mgmt: dict) -> tuple[list[str], list[str], list[str]]:
    """Změna adresy managementu na bridgi. Návrat (revert) se na konci obrátí – proto
    v revert pořadí „od posledního kroku“."""
    dhcp = [c for c in _rows(inv, "dhcp_client") if str(c.get("interface")) == BRIDGE and not _true(c.get("disabled"))]
    static = [x for x in _rows(inv, "addresses") if str(x.get("interface")) == BRIDGE and not _true(x.get("dynamic"))
              and not _true(x.get("disabled"))]
    route = next((x for x in _rows(inv, "routes_default") if str(x.get("comment") or "") == "mtc mgmt"), None)
    apply: list[str] = []
    undo: list[str] = []      # kroky návratu v pořadí, v jakém se mají provést
    changes: list[str] = []
    if mgmt.get("mode") == "static":
        addr, gw = mgmt["address"], mgmt.get("gateway")
        if not any(str(x.get("address")) == addr for x in static):
            apply.append(f'/ip address add address={q(addr)} interface={BRIDGE} comment="mtc"')
            undo.append(f"/ip address remove [find address={q(addr)} interface={BRIDGE}]")
            changes.append(f"management: statická adresa {addr}")
        for x in static:
            if str(x.get("address")) != addr:
                apply.append(f"/ip address remove [find address={q(str(x.get('address')))} interface={BRIDGE}]")
                undo.insert(0, f'/ip address add address={q(str(x.get("address")))} interface={BRIDGE} comment="mtc"')
        if gw and (not route or str(route.get("gateway")) != gw):
            if route:
                apply.append(f'/ip route set [find comment="mtc mgmt"] gateway={q(gw)}')
                undo.insert(0, f'/ip route set [find comment="mtc mgmt"] gateway={q(str(route.get("gateway")))}')
            else:
                apply.append(f'/ip route add dst-address=0.0.0.0/0 gateway={q(gw)} comment="mtc mgmt"')
                undo.insert(0, '/ip route remove [find comment="mtc mgmt"]')
        if mgmt.get("dns"):
            apply.append(f"/ip dns set servers={q(','.join(mgmt['dns']))}")
        if dhcp:
            apply.append(f"/ip dhcp-client remove [find interface={BRIDGE}]")
            undo.insert(0, f'/ip dhcp-client add interface={BRIDGE} use-peer-dns=yes add-default-route=yes comment="mtc" disabled=no')
            changes.append("management: DHCP klient pryč")
    else:
        if not dhcp:
            apply.append(f'/ip dhcp-client add interface={BRIDGE} use-peer-dns=yes add-default-route=yes comment="mtc" disabled=no')
            undo.append(f"/ip dhcp-client remove [find interface={BRIDGE}]")
            changes.append("management: DHCP")
            for x in static:
                apply.append(f"/ip address remove [find address={q(str(x.get('address')))} interface={BRIDGE}]")
                undo.insert(0, f'/ip address add address={q(str(x.get("address")))} interface={BRIDGE} comment="mtc"')
            if route:
                apply.append('/ip route remove [find comment="mtc mgmt"]')
                undo.insert(0, f'/ip route add dst-address=0.0.0.0/0 gateway={q(str(route.get("gateway")))} comment="mtc mgmt"')
    # reconcile() návrat na konci obrací – vložíme v opačném pořadí, než se má provést
    return apply, list(reversed(undo)), changes


def _cur_item(b: dict) -> dict:
    return {"pvid": int(str(b.get("pvid") or "1")), "frame_types": str(b.get("frame-types") or FT_ALL),
            "mode": "trunk", "trusted": _true(b.get("trusted")), "comment": None}


def basis(inv: dict) -> str:
    """Otisk částí, které sladění mění (plán propadne, když se změní)."""
    keep = {
        "bridge_ports": sorted(([str(b.get(k)) for k in ("bridge", "interface", "pvid", "frame-types", "trusted", "disabled")]
                                for b in _rows(inv, "bridge_ports") if not _true(b.get("dynamic"))), key=str),
        "bridge_vlans": sorted(([str(v.get(k)) for k in ("bridge", "vlan-ids", "tagged", "untagged", "disabled")]
                                for v in _rows(inv, "bridge_vlans") if not _true(v.get("dynamic"))), key=str),
        "ethernet": sorted(([str(e.get(k)) for k in ("default-name", "name", "loop-protect", "disabled")]
                            for e in _rows(inv, "ethernet")), key=str),
    }
    return hashlib.sha256(json.dumps(keep, sort_keys=True).encode()).hexdigest()
