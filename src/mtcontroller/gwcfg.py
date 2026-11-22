"""Řízená brána (gateway mode, krok 1): WAN, L3 sítí lokality, DHCP, NAT a základní zónový firewall.

Brána je řízené zařízení jako switch (stejné převzetí se zálohou a návratem, stejný bridge
s VLAN filtrováním), jen navíc:

- WAN port není v bridgi; internet přes DHCP, statickou adresu nebo PPPoE (volitelně ve VLAN),
- každá síť lokality s podsítí má na bráně adresu (síť managementu přímo na bridgi, ostatní
  na rozhraní `vlan<id>`), DHCP server a DNS,
- zóny podle účelu sítě (MGMT, LAN, GUEST, IOT, CAM, VOIP, OTHER) + WAN; výchozí politiky:
  každá zóna do internetu, MGMT a LAN i do ostatních sítí, zbytek mezi sebou ne (fwmatrix),
- maškaráda na WAN.

Heslo PPPoE model nenese (model se ukládá do plánu) – doplní se až při vydání bootstrapu.
Pojmenování: adresa/pool/DHCP server sítě `mtc-v<id>`, rozhraní `vlan<id>`, WAN objekty
`mtc WAN` / `wan-vlan` / `pppoe-wan`.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from . import fwmatrix
from .routeros import q

BRIDGE = "bridge"
WAN_ZONE = "WAN"
WAN_MODES = ("dhcp", "static", "pppoe")
WAN_VLAN_IF = "wan-vlan"
PPPOE_IF = "pppoe-wan"
SECRET_MASK = "********"
ZONE_OF = {"mgmt": "MGMT", "lan": "LAN", "hoste": "GUEST", "iot": "IOT", "kamery": "CAM", "voip": "VOIP", "jine": "OTHER"}
# zóny, které smějí do všech ostatních vnitřních sítí
TRUSTED = ("MGMT", "LAN")
LEASE_RE = re.compile(r"^\d{1,3}[mhd]$")
LEASE_DEFAULT = {"GUEST": "2h"}
USER_RE = re.compile(r"^[\x21-\x7e]{1,64}$")
FW_PREFIX = "mtc-fw: "


class GatewayError(ValueError):
    pass


def _ip(v: Any, what: str) -> str:
    try:
        return str(ipaddress.IPv4Address(str(v).strip()))
    except ValueError:
        raise GatewayError(f"{what}: neplatná IPv4 adresa") from None


def validate(data: dict, old: dict | None = None) -> dict:
    """Nastavení brány z UI → uložená podoba (bez hesla; to jde do tajemství zařízení)."""
    old = old or {}
    w = data.get("wan") or {}
    mode = str(w.get("mode") or "dhcp")
    if mode not in WAN_MODES:
        raise GatewayError("WAN: režim dhcp, static nebo pppoe")
    wan: dict[str, Any] = {"port": str(w.get("port") or "").strip()[:32] or None, "mode": mode, "vlan": None}
    if w.get("vlan") not in (None, "", 0, "0"):
        try:
            vid = int(w["vlan"])
        except (TypeError, ValueError):
            raise GatewayError("WAN: VLAN je číslo 2–4094") from None
        if not 2 <= vid <= 4094:
            raise GatewayError("WAN: VLAN je číslo 2–4094")
        wan["vlan"] = vid
    if mode == "static":
        try:
            addr = ipaddress.IPv4Interface(str(w.get("address") or "").strip())
        except ValueError:
            raise GatewayError("WAN: adresa ve tvaru 203.0.113.10/24") from None
        gw = _ip(w.get("gateway"), "WAN brána")
        dns = [_ip(x, "WAN DNS") for x in (w.get("dns") or []) if str(x).strip()][:3]
        if not dns:
            raise GatewayError("WAN: u statické adresy zadej aspoň jeden DNS server")
        wan.update(address=str(addr), gateway=gw, dns=dns)
    if mode == "pppoe":
        user = str(w.get("user") or "").strip()
        if not USER_RE.match(user):
            raise GatewayError("WAN: jméno PPPoE (bez mezer a diakritiky, max 64 znaků)")
        wan["user"] = user
    dhcp: dict[str, dict] = {}
    for vid, o in (data.get("dhcp") or {}).items():
        if not str(vid).isdigit() or not isinstance(o, dict):
            continue
        item: dict[str, Any] = {"enabled": bool(o.get("enabled", True))}
        if o.get("start") or o.get("end"):
            a, b = ipaddress.IPv4Address(_ip(o.get("start"), f"DHCP VLAN {vid} od")), \
                ipaddress.IPv4Address(_ip(o.get("end"), f"DHCP VLAN {vid} do"))
            if a > b:
                raise GatewayError(f"DHCP VLAN {vid}: začátek rozsahu je za koncem")
            item.update(start=str(a), end=str(b))
        if o.get("lease"):
            if not LEASE_RE.match(str(o["lease"])):
                raise GatewayError(f"DHCP VLAN {vid}: doba zápůjčky např. 30m, 8h, 1d")
            item["lease"] = str(o["lease"])
        dhcp[str(int(vid))] = item
    return {"device_id": old.get("device_id"), "wan": wan, "dhcp": dhcp}


def _ranges(net: ipaddress.IPv4Network, gw: ipaddress.IPv4Address, o: dict) -> str | None:
    """Rozsah DHCP: zadaný, jinak horní část sítě (u /24 od .100); adresa brány se vynechá."""
    hosts = net.num_addresses - 2
    if hosts < 4:
        return None
    if o.get("start") and o.get("end"):
        a, b = ipaddress.IPv4Address(o["start"]), ipaddress.IPv4Address(o["end"])
        if a not in net or b not in net:
            raise GatewayError(f"rozsah DHCP {a}–{b} neleží v síti {net}")
    else:
        skip = 99 if hosts >= 200 else (hosts // 4 if hosts >= 16 else 1)
        a, b = net.network_address + 1 + skip, net.broadcast_address - 1
    a, b = max(a, net.network_address + 1), min(b, net.broadcast_address - 1)
    if a <= gw <= b:
        parts = [(a, gw - 1), (gw + 1, b)]
    else:
        parts = [(a, b)]
    return ",".join(f"{x}-{y}" for x, y in parts if x <= y) or None


def plan(intent: dict, gw: dict | None, wan_port: str | None, mg_vid: int,
         extra_admin: list[str] | None = None) -> tuple[dict, list[str], list[str]]:
    """Část modelu `gw` + překážky + varování."""
    gw = gw or {}
    blockers: list[str] = []
    warnings: list[str] = []
    w = dict(gw.get("wan") or {"mode": "dhcp", "vlan": None})
    w["port"] = wan_port
    w["configured"] = bool(gw.get("device_id"))
    if not wan_port:
        blockers.append("brána: vyber WAN port (Nastavení brány)")
    l3 = wan_port or ""
    wan_ifs = [l3] if l3 else []
    if w.get("vlan"):
        l3 = WAN_VLAN_IF
        wan_ifs.append(l3)
    if w.get("mode") == "pppoe":
        l3 = PPPOE_IF
        wan_ifs.append(l3)
    w["iface"] = l3
    nets: list[dict] = []
    zones: dict[str, list[str]] = {WAN_ZONE: wan_ifs}
    seen: list[ipaddress.IPv4Network] = []
    for n in intent["networks"]:
        vid = int(n["vlan_id"])
        if not n.get("subnet"):
            warnings.append(f"síť {n['name']} (VLAN {vid}) nemá podsíť – brána ji nebude směrovat ani jí dávat DHCP")
            continue
        try:
            net = ipaddress.IPv4Network(str(n["subnet"]), strict=False)
        except ValueError:
            blockers.append(f"síť {n['name']}: neplatná podsíť {n['subnet']}")
            continue
        if any(net.overlaps(s) for s in seen):
            blockers.append(f"síť {n['name']}: podsíť {net} se překrývá s jinou sítí lokality")
            continue
        seen.append(net)
        try:
            addr = ipaddress.IPv4Address(str(n["gateway"])) if n.get("gateway") else net.network_address + 1
        except ValueError:
            addr = None
        if addr is None or addr not in net or addr in (net.network_address, net.broadcast_address):
            blockers.append(f"síť {n['name']}: brána {n.get('gateway')} neleží v podsíti {net}")
            continue
        zone = ZONE_OF.get(str(n.get("purpose") or "lan"), "OTHER")
        iface = BRIDGE if vid == mg_vid else f"vlan{vid}"
        o = (gw.get("dhcp") or {}).get(str(vid)) or {}
        dhcp = None
        if o.get("enabled", True):
            try:
                rng = _ranges(net, addr, o)
            except GatewayError as e:
                blockers.append(f"síť {n['name']}: {e}")
                rng = None
            if rng:
                dns = [d for d in (n.get("dns") or []) if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", str(d))] or [str(addr)]
                dhcp = {"ranges": rng, "lease": o.get("lease") or LEASE_DEFAULT.get(zone, "12h"), "dns": dns}
            else:
                warnings.append(f"síť {n['name']}: podsíť {net} je na DHCP moc malá – bez DHCP serveru")
        nets.append({"vid": vid, "name": n["name"], "iface": iface, "address": f"{addr}/{net.prefixlen}",
                     "network": str(net), "gateway": str(addr), "zone": zone, "dhcp": dhcp})
        zones.setdefault(zone, []).append(iface)
    if not nets:
        blockers.append("brána: žádná síť lokality nemá podsíť – doplň ji v sítích (Nastavení → Sítě)")
    if not any(n["vid"] == mg_vid for n in nets):
        blockers.append("brána: síť managementu musí mít podsíť (brána v ní dostane adresu)")
    inner = [z for z in zones if z != WAN_ZONE]
    policies = [{"src": z, "dst": WAN_ZONE, "action": "accept"} for z in inner]
    policies += [{"src": s, "dst": d, "action": "accept"} for s in inner if s in TRUSTED for d in inner if d != s]
    admin_zone = next((z for z in TRUSTED if z in zones), None)
    admin = [n["network"] for n in nets if n["zone"] == admin_zone]
    if not admin:
        warnings.append("žádná síť s účelem management ani LAN – správa brány (Winbox, SSH) půjde jen z controlleru")
    admin += [a for a in extra_admin or [] if a not in admin]
    if w.get("mode") == "static":
        try:
            wn = ipaddress.IPv4Interface(w["address"]).network
            if any(wn.overlaps(s) for s in seen):
                blockers.append(f"WAN adresa {w['address']} leží v některé síti lokality")
        except (KeyError, ValueError):
            blockers.append("WAN: chybí statická adresa")
    return ({"wan": w, "nets": nets, "zones": zones, "policies": policies, "admin": admin,
             "admin_zone": admin_zone}, blockers, warnings)


# --- čistá konfigurace (bootstrap při převzetí) -----------------------------------------------------

def _net_lines(n: dict) -> list[str]:
    """L3 jedné sítě: rozhraní, adresa, DHCP. (Členství v zóně řeší firewall / sladění.)"""
    L: list[str] = []
    vid = int(n["vid"])
    if n["iface"] != BRIDGE:
        L.append(f'/interface vlan add name={q(n["iface"])} interface={BRIDGE} vlan-id={vid} comment="mtc"')
    L.append(f'/ip address add address={q(n["address"])} interface={q(n["iface"])} comment={q("mtc v" + str(vid))}')
    if n.get("dhcp"):
        L += _dhcp_lines(n)
    return L


def _dhcp_lines(n: dict) -> list[str]:
    name, d = f"mtc-v{int(n['vid'])}", n["dhcp"]
    return [
        f'/ip pool add name={name} ranges={q(d["ranges"])} comment="mtc"',
        f'/ip dhcp-server add name={name} interface={q(n["iface"])} address-pool={name} lease-time={d["lease"]} '
        f'comment="mtc" disabled=no',
        f'/ip dhcp-server network add address={q(n["network"])} gateway={q(n["gateway"])} '
        f'dns-server={q(",".join(d["dns"]))} comment="mtc"',
    ]


def wan_lines(w: dict, secrets: dict | None = None, peer_dns: bool = True) -> list[str]:
    port, L = w["port"], []
    base = port
    if w.get("vlan"):
        L.append(f'/interface vlan add name={WAN_VLAN_IF} interface={q(port)} vlan-id={int(w["vlan"])} comment="mtc WAN"')
        base = WAN_VLAN_IF
    pd = "yes" if peer_dns else "no"
    if w["mode"] == "dhcp":
        L.append(f'/ip dhcp-client add interface={q(base)} use-peer-dns={pd} use-peer-ntp=no add-default-route=yes '
                 f'comment="mtc WAN" disabled=no')
    elif w["mode"] == "static":
        L.append(f'/ip address add address={q(w["address"])} interface={q(base)} comment="mtc WAN"')
        L.append(f'/ip route add dst-address=0.0.0.0/0 gateway={q(w["gateway"])} comment="mtc WAN"')
        if peer_dns:
            L.append(f'/ip dns set servers={q(",".join(w["dns"]))}')
    else:
        pw = (secrets or {}).get("wan_password")
        L.append(f'/interface pppoe-client add name={PPPOE_IF} interface={q(base)} user={q(w["user"])} '
                 f'password={q(pw) if pw is not None else q(SECRET_MASK)} add-default-route=yes use-peer-dns={pd} '
                 f'comment="mtc WAN" disabled=no')
    return L


def config_lines(model: dict, secrets: dict | None = None) -> list[str]:
    """Část bootstrapu za bridgem a VLAN tabulkou: WAN, sítě, DNS, firewall."""
    g = model["gw"]
    dns = model.get("dns_servers")
    L = ["# --- brana: WAN ---"] + wan_lines(g["wan"], secrets, peer_dns=not dns)
    if dns:
        L.append(f'/ip dns set servers={q(",".join(dns))}')
    L.append("# --- brana: site lokality (adresa, DHCP) ---")
    for n in g["nets"]:
        L += _net_lines(n)
    L.append("/ip dns set allow-remote-requests=yes")
    L.append(fwmatrix.render(g["zones"], g["policies"], g["admin"]).rstrip())
    # MAC Winbox jen ze sítě správy, sousedy (LLDP/MNDP) neohlašovat do internetu
    L.append(f'/tool mac-server mac-winbox set allowed-interface-list={q(g["admin_zone"]) if g.get("admin_zone") else "none"}')
    L.append(f":do {{ /ip neighbor discovery-settings set discover-interface-list=!{WAN_ZONE} }} on-error={{}}")
    return L


# --- sladění řízené brány se záměrem ----------------------------------------------------------------

def _rows(inv: dict, key: str) -> list[dict]:
    v = inv.get(key)
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def _fw_place(chain: str) -> str:
    return f'place-before=[find comment="{FW_PREFIX}{"input" if chain == "input" else "forward"} vychozi zakaz"]'


def _zone_rules(zone: str) -> list[str]:
    """Pravidla nové vnitřní zóny (služby routeru, provoz uvnitř zóny) – před výchozí zákaz."""
    f = "/ip firewall filter add"
    L = [f'{f} chain=input action=accept in-interface-list={q(zone)} protocol={proto} dst-port={ports} '
         f'comment="{FW_PREFIX}input sluzby {zone}" {_fw_place("input")}' for proto, ports in fwmatrix.SERVICES]
    L.append(f'{f} chain=forward action=accept in-interface-list={q(zone)} out-interface-list={q(zone)} '
             f'comment="{FW_PREFIX}{zone} -> {zone}" {_fw_place("forward")}')
    return L


def _policy_line(p: dict) -> str:
    return (f'/ip firewall filter add chain=forward action={p["action"]} in-interface-list={q(p["src"])} '
            f'out-interface-list={q(p["dst"])} comment="{FW_PREFIX}{p["src"]} -> {p["dst"]}" {_fw_place("forward")}')


def reconcile(inv: dict, model: dict) -> tuple[list[str], list[str], list[str]]:
    """Rozdíl L3 části brány proti inventuře: (kroky, návrat v pořadí kroků, popis změn).

    Řeší sítě (rozhraní, adresa, DHCP), členství v zónách, nové/zaniklé zóny a politiky mezi
    zónami. WAN se po převzetí nemění (změna WAN = nové převzetí; přijde s dual-WAN)."""
    g = model["gw"]
    pairs: list[tuple[str, str]] = []
    changes: list[str] = []
    addrs = [a for a in _rows(inv, "addresses") if str(a.get("dynamic")) not in ("true", "True")]
    vlan_if = {str(v.get("name")): v for v in _rows(inv, "vlans")}
    pools = {str(p.get("name")): p for p in _rows(inv, "ip_pools")}
    servers = {str(s.get("name")): s for s in _rows(inv, "dhcp_servers")}
    nets_cur = {str(x.get("address")): x for x in _rows(inv, "dhcp_networks")}
    lists = {str(x.get("name")) for x in _rows(inv, "if_lists")}
    members = {(str(m.get("list")), str(m.get("interface"))) for m in _rows(inv, "if_list_members")}
    fw = [r for r in _rows(inv, "fw_filter") if str(r.get("comment") or "").startswith(FW_PREFIX)]
    if not any(str(r.get("comment")) == f"{FW_PREFIX}forward vychozi zakaz" for r in fw):
        # firewall brány na zařízení není (nebylo převzato jako brána) – nic neslaďovat naslepo
        return [], [], []
    wan_ifs = set(g["zones"].get(WAN_ZONE) or [])
    want_ifaces = {n["iface"] for n in g["nets"]}
    new_zones = [z for z in g["zones"] if z not in lists]
    for z in new_zones:
        pairs.append((f'/interface list add name={q(z)} comment="mtc-fw zona"', f"/interface list remove [find name={q(z)}]"))
        for ln in _zone_rules(z):
            pairs.append((ln, ""))
        for c in (f"{FW_PREFIX}input sluzby {z}", f"{FW_PREFIX}{z} -> {z}"):
            pairs.append(("", f"/ip firewall filter remove [find comment={q(c)}]"))
        changes.append(f"nová zóna {z}")
    # DHCP servery, které už být nemají (zrušená síť, vypnuté DHCP, `mtc-lan` z výchozí konfigurace) –
    # dřív než nové: na jednom rozhraní smí být jen jeden server
    want_srv = {f"mtc-v{int(n['vid'])}": n for n in g["nets"] if n.get("dhcp")}
    for name, srv in sorted(servers.items()):
        if not re.fullmatch(r"mtc-v\d+|mtc-lan", name) or name in want_srv:
            continue
        iface = str(srv.get("interface"))
        net_key = None
        for a in addrs:
            if str(a.get("interface")) == iface:
                try:
                    k = str(ipaddress.IPv4Interface(str(a.get("address"))).network)
                except ValueError:
                    continue
                net_key = k if k in nets_cur else net_key
        pairs += _drop_dhcp(name, srv, pools.get(name), net_key, nets_cur)
        nets_cur = {k: v for k, v in nets_cur.items() if k != net_key}
        changes.append(f"DHCP server {name} na {iface}: zrušit")
    for n in g["nets"]:
        vid, iface, name = int(n["vid"]), n["iface"], f"mtc-v{int(n['vid'])}"
        cur_a = next((a for a in addrs if str(a.get("interface")) == iface), None)
        fresh = cur_a is None
        if iface != BRIDGE and iface not in vlan_if:
            pairs.append((f'/interface vlan add name={q(iface)} interface={BRIDGE} vlan-id={vid} comment="mtc"',
                          f"/interface vlan remove [find name={q(iface)}]"))
        if fresh:
            pairs.append((f'/ip address add address={q(n["address"])} interface={q(iface)} comment={q("mtc v" + str(vid))}',
                          f"/ip address remove [find interface={q(iface)} address={q(n['address'])}]"))
            changes.append(f"síť {n['name']} (VLAN {vid}): adresa {n['address']}")
        elif str(cur_a.get("address")) != n["address"]:
            old = str(cur_a.get("address"))
            pairs.append((f"/ip address set [find interface={q(iface)} address={q(old)}] address={q(n['address'])}",
                          f"/ip address set [find interface={q(iface)} address={q(n['address'])}] address={q(old)}"))
            changes.append(f"síť {n['name']}: adresa {old} → {n['address']}")
        d, srv = n.get("dhcp"), servers.get(name)
        if d and not srv:
            for ln in _dhcp_lines(n):
                pairs.append((ln, ""))
            pairs.append(("", f"/ip pool remove [find name={name}]"))
            pairs.append(("", f"/ip dhcp-server remove [find name={name}]"))
            pairs.append(("", f"/ip dhcp-server network remove [find address={q(n['network'])}]"))
            changes.append(f"síť {n['name']}: DHCP {d['ranges']}")
        elif d and srv:
            cp = str((pools.get(name) or {}).get("ranges") or "")
            if cp != d["ranges"]:
                pairs.append((f"/ip pool set [find name={name}] ranges={q(d['ranges'])}",
                              f"/ip pool set [find name={name}] ranges={q(cp)}"))
                changes.append(f"síť {n['name']}: rozsah DHCP {cp} → {d['ranges']}")
            cl = str(srv.get("lease-time") or "")
            if cl and _secs(cl) != _secs(d["lease"]):
                pairs.append((f"/ip dhcp-server set [find name={name}] lease-time={d['lease']}",
                              f"/ip dhcp-server set [find name={name}] lease-time={cl}"))
                changes.append(f"síť {n['name']}: doba zápůjčky {cl} → {d['lease']}")
            cn = nets_cur.get(n["network"])
            want_dns = ",".join(d["dns"])
            if cn is None:
                old_net = next((k for k, x in nets_cur.items() if str(x.get("gateway")) == str((cur_a or {}).get("address", "")).split("/")[0]), None)
                if old_net:
                    pairs.append((f"/ip dhcp-server network set [find address={q(old_net)}] address={q(n['network'])} "
                                  f"gateway={q(n['gateway'])} dns-server={q(want_dns)}",
                                  f"/ip dhcp-server network set [find address={q(n['network'])}] address={q(old_net)} "
                                  f"gateway={q(str(nets_cur[old_net].get('gateway') or ''))} "
                                  f"dns-server={q(str(nets_cur[old_net].get('dns-server') or ''))}"))
                else:
                    pairs.append((f'/ip dhcp-server network add address={q(n["network"])} gateway={q(n["gateway"])} '
                                  f'dns-server={q(want_dns)} comment="mtc"',
                                  f"/ip dhcp-server network remove [find address={q(n['network'])}]"))
            elif str(cn.get("gateway") or "") != n["gateway"] or str(cn.get("dns-server") or "") != want_dns:
                pairs.append((f"/ip dhcp-server network set [find address={q(n['network'])}] gateway={q(n['gateway'])} "
                              f"dns-server={q(want_dns)}",
                              f"/ip dhcp-server network set [find address={q(n['network'])}] "
                              f"gateway={q(str(cn.get('gateway') or ''))} dns-server={q(str(cn.get('dns-server') or ''))}"))
                changes.append(f"síť {n['name']}: DNS/brána v DHCP")
        if (n["zone"], iface) not in members:
            for lst, i in sorted(members):
                if i == iface and lst != n["zone"] and lst in ZONE_OF.values():
                    pairs.append((f"/interface list member remove [find list={q(lst)} interface={q(iface)}]",
                                  f'/interface list member add list={q(lst)} interface={q(iface)} comment="mtc-fw"'))
            pairs.append((f'/interface list member add list={q(n["zone"])} interface={q(iface)} comment="mtc-fw"',
                          f"/interface list member remove [find list={q(n['zone'])} interface={q(iface)}]"))
            if not fresh:
                changes.append(f"síť {n['name']}: zóna {n['zone']}")
    # sítě, které ze záměru zmizely (naše rozhraní vlan<id> nad bridgem)
    for iface in sorted(vlan_if):
        if not re.fullmatch(r"vlan\d+", iface) or iface in want_ifaces or iface in wan_ifs:
            continue
        if str(vlan_if[iface].get("interface")) != BRIDGE:
            continue
        for lst, i in sorted(members):
            if i == iface:
                pairs.append((f"/interface list member remove [find list={q(lst)} interface={q(iface)}]",
                              f'/interface list member add list={q(lst)} interface={q(iface)} comment="mtc-fw"'))
        for a in addrs:
            if str(a.get("interface")) == iface:
                pairs.append((f"/ip address remove [find interface={q(iface)}]",
                              f'/ip address add address={q(str(a.get("address")))} interface={q(iface)} comment="mtc"'))
        pairs.append((f"/interface vlan remove [find name={q(iface)}]",
                      f'/interface vlan add name={q(iface)} interface={BRIDGE} '
                      f'vlan-id={q(str(vlan_if[iface].get("vlan-id")))} comment="mtc"'))
        changes.append(f"zrušit síť na {iface}")
    # politiky mezi zónami (různé zóny; pravidla uvnitř zóny a služby drží _zone_rules)
    cur_pol: dict[tuple[str, str], str] = {}
    for r in fw:
        m = re.fullmatch(re.escape(FW_PREFIX) + r"([A-Z][A-Z0-9_-]*) -> ([A-Z][A-Z0-9_-]*)", str(r.get("comment")))
        if m and m.group(1) != m.group(2) and str(r.get("chain")) == "forward":
            cur_pol[(m.group(1), m.group(2))] = str(r.get("action"))
    want_pol = {(p["src"], p["dst"]): p["action"] for p in g["policies"]}
    for (s, d), act in want_pol.items():
        c = f"{FW_PREFIX}{s} -> {d}"
        if (s, d) not in cur_pol:
            pairs.append((_policy_line({"src": s, "dst": d, "action": act}),
                          f"/ip firewall filter remove [find comment={q(c)}]"))
            changes.append(f"firewall: {s} → {d} {'povolit' if act == 'accept' else 'zakázat'}")
        elif cur_pol[(s, d)] != act:
            pairs.append((f"/ip firewall filter set [find comment={q(c)}] action={act}",
                          f"/ip firewall filter set [find comment={q(c)}] action={cur_pol[(s, d)]}"))
            changes.append(f"firewall: {s} → {d} {'povolit' if act == 'accept' else 'zakázat'}")
    for (s, d), act in sorted(cur_pol.items()):
        if (s, d) not in want_pol:
            c = f"{FW_PREFIX}{s} -> {d}"
            pairs.append((f"/ip firewall filter remove [find comment={q(c)}]",
                          _policy_line({"src": s, "dst": d, "action": act})))
            changes.append(f"firewall: zrušit pravidlo {s} → {d}")
    # kdo smí spravovat router (address-list mtc-admin); bez sekce v inventuře (starší data) neřešit
    if isinstance(inv.get("fw_admin"), list):
        cur_adm = {str(x.get("address")) for x in _rows(inv, "fw_admin") if str(x.get("dynamic")) not in ("true", "True")}
        for a in g["admin"]:
            if a not in cur_adm:
                pairs.append((f'/ip firewall address-list add list={fwmatrix.ADMIN_LIST} address={q(a)} '
                              f'comment="{FW_PREFIX}sprava routeru"',
                              f"/ip firewall address-list remove [find list={fwmatrix.ADMIN_LIST} address={q(a)}]"))
                changes.append(f"správa routeru povolena z {a}")
        for a in sorted(cur_adm - set(g["admin"])):
            pairs.append((f"/ip firewall address-list remove [find list={fwmatrix.ADMIN_LIST} address={q(a)}]",
                          f'/ip firewall address-list add list={fwmatrix.ADMIN_LIST} address={q(a)} '
                          f'comment="{FW_PREFIX}sprava routeru"'))
            changes.append(f"správa routeru už ne z {a}")
    apply = [a for a, _ in pairs if a]
    revert = [r for _, r in pairs if r]
    return apply, revert, changes


def _drop_dhcp(name: str, srv: dict, pool: dict | None, net_key: str | None, nets_cur: dict) -> list[tuple[str, str]]:
    out = [(f"/ip dhcp-server remove [find name={name}]",
            f'/ip dhcp-server add name={name} interface={q(str(srv.get("interface")))} address-pool={name} '
            f'lease-time={srv.get("lease-time") or "12h"} comment="mtc" disabled=no')]
    if pool:
        # návrat jde v opačném pořadí: pool se musí vrátit dřív než server, proto je uveden až za ním
        out.append((f"/ip pool remove [find name={name}]",
                    f'/ip pool add name={name} ranges={q(str(pool.get("ranges") or ""))} comment="mtc"'))
    if net_key:
        x = nets_cur[net_key]
        out.append((f"/ip dhcp-server network remove [find address={q(net_key)}]",
                    f'/ip dhcp-server network add address={q(net_key)} gateway={q(str(x.get("gateway") or ""))} '
                    f'dns-server={q(str(x.get("dns-server") or ""))} comment="mtc"'))
    return out


def _secs(v: str) -> int:
    """Doba RouterOS (`12h`, `1d`, `00:30:00`, `1d2h`) na sekundy – pro porovnání."""
    v = str(v)
    if ":" in v:
        days = 0
        m = re.match(r"(?:(\d+)w)?(?:(\d+)d)?(.*)", v)
        if m:
            days = int(m.group(1) or 0) * 7 + int(m.group(2) or 0)
            v = m.group(3)
        h, mi, s = (v.split(":") + ["0", "0"])[:3]
        return days * 86400 + int(h or 0) * 3600 + int(mi or 0) * 60 + int(s or 0)
    total = 0
    for num, unit in re.findall(r"(\d+)([wdhms])", v):
        total += int(num) * {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    return total
