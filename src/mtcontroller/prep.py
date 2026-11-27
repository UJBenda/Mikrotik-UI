"""Příprava zařízení před nasazením (Zařízení → Připravit zařízení).

Technik si pro kus, který ještě není ve správě, předem nakliká porty, VLAN a správu. Z návrhu vznikne skript
(reset na čistou konfiguraci + naše nastavení + ohlášení controlleru). Po zapojení na lokalitě se zařízení
ohlásí klíčem lokality, čeká na schválení a spáruje se s návrhem (podle sériového čísla, jinak podle modelu).

Konfiguraci switche generuje stejný kód jako převzetí do řízení (`switchcfg.config_body`) – připravené zařízení
tak po adopci vypadá, jako by ho controller už převzal, a řízení ho nemusí přestavovat.

Zatím jen role switch. Porty se berou z katalogu modelů: co controller viděl na zařízeních zákazníka
(přesná výchozí jména portů), pár vestavěných modelů a ruční zadání počtu portů.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

from . import defaults, dhcpguard, intent as intentmod, inventory, regional, switchcfg
from .db import Database, Tx, operator_scope, tenant_scope
from .routeros import SCRIPT_POLICY, FetchOpts, q, q_source
from .services import NotFound, ServiceError, audit, token_hash

MODES = ("access", "hybrid", "trunk", "off")
POE = ("auto-on", "off", "forced-on")
PORT_NAME = re.compile(r"^(ether[0-9]{1,2}|sfp[0-9]{1,2}|sfp-sfpplus[0-9]{1,2}|sfp28-[0-9]{1,2}|qsfp28-[0-9]{1,2}-[0-9]|qsfpplus[0-9]{1,2}-[0-9]|combo[0-9]{1,2})$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,59}$")
SERIAL_RE = re.compile(r"^[A-Za-z0-9]{6,20}$")

# vestavěné modely (jména portů podle výchozí konfigurace RouterOS); další se učí ze zařízení zákazníka
BUILTIN: dict[str, dict] = {
    "CRS328-24P-4S+": {"ports": [f"ether{i}" for i in range(1, 25)] + [f"sfp-sfpplus{i}" for i in range(1, 5)],
                       "poe": [f"ether{i}" for i in range(1, 25)]},
    "CRS326-24G-2S+": {"ports": [f"ether{i}" for i in range(1, 25)] + [f"sfp-sfpplus{i}" for i in range(1, 3)], "poe": []},
}


def _rows(inv: dict, key: str) -> list[dict]:
    v = inv.get(key)
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def model_key(name: Any) -> str:
    """„CRS328-24P-4S+RM“ i „CRS328-24P-4S+“ jsou tentýž model (RM = montáž do racku)."""
    return re.sub(r"(RM|IN)$", "", str(name or "").strip())


def catalog(tx: Tx) -> list[dict]:
    """Modely, pro které známe porty: ze zařízení zákazníka (přesně), pak vestavěné."""
    out: dict[str, dict] = {}
    for d in tx.fetchall("""SELECT id, model FROM device WHERE model IS NOT NULL AND model <> ''
                            AND state IN ('adopted', 'offline') ORDER BY created_at"""):
        k = model_key(d["model"])
        if not k or k in out:
            continue
        inv = inventory.load(tx, d["id"], ["ethernet", "interfaces"])
        ports = [p["port"] for p in switchcfg.physical_ports(inv) if PORT_NAME.match(p["port"])]
        if len(ports) < 2:
            continue
        poe = [str(e.get("default-name") or e.get("name")) for e in _rows(inv, "ethernet") if e.get("poe-out") is not None]
        out[k] = {"model": k, "ports": ports, "poe": [p for p in poe if p in ports], "source": "device"}
    for k, v in BUILTIN.items():
        out.setdefault(k, {"model": k, **v, "source": "builtin"})
    return sorted(out.values(), key=lambda m: m["model"])


def _guess_mgmt(observed: list[dict], invs: dict[str, dict]) -> dict | None:
    """Lokalita mimo řízení: VLAN správy = síť, ve které má adresu nejvíc zařízení (brána má adresu ve všech,
    switche a AP jen ve správě). native = False, když ji zařízení mají na VLAN rozhraní (na trunku jde s tagem)."""
    best = None
    for o in observed:
        vid = o.get("vlan_id")
        if not vid or not o.get("subnet"):
            continue
        try:
            net = ipaddress.ip_network(str(o["subnet"]))
        except ValueError:
            continue
        count, on_vlan = 0, 0
        for inv in invs.values():
            vlan_ifaces = {str(v.get("name")) for v in _rows(inv, "vlans") if str(v.get("vlan-id")) == str(vid)}
            hit = None
            for a in _rows(inv, "addresses"):
                try:
                    if ipaddress.ip_interface(str(a.get("address"))).ip in net:
                        hit = str(a.get("interface") or "")
                        break
                except ValueError:
                    continue
            if hit is not None:
                count += 1
                on_vlan += hit in vlan_ifaces
        if count >= 2 and (best is None or count > best["count"]):
            best = {"vid": int(vid), "subnet": o["subnet"], "count": count, "native": int(vid) == 1 or on_vlan * 2 < count}
    return best


def _site_stp(invs: dict[str, dict]) -> dict:
    """Co má lokalita na bridgích: převažující režim STP a jméno/revize MSTP regionu (pokud ho inventura zná)."""
    modes: dict[str, int] = {}
    region, revision = "", 0
    for inv in invs.values():
        for b in _rows(inv, "bridges"):
            mode = str(b.get("protocol-mode") or "none")
            if str(b.get("disabled")).lower() == "true" or mode not in ("rstp", "mstp"):
                continue
            modes[mode] = modes.get(mode, 0) + 1
            if mode == "mstp" and not region and b.get("region-name"):
                region = str(b["region-name"])
                revision = int(b["region-revision"]) if str(b.get("region-revision") or "").isdigit() else 0
    mode = "mstp" if modes.get("mstp", 0) > modes.get("rstp", 0) else "rstp"
    return {"mode": mode, "region": region if mode == "mstp" else "", "revision": revision if mode == "mstp" else 0}


def context(tx: Tx, site_id: Any) -> dict:
    """Co průvodce potřebuje vědět o lokalitě: sítě (záměr, jinak zjištěné na zařízeních), VLAN správy, modely, profily."""
    sid = str(site_id)
    cur = tx.fetchone("SELECT data FROM site_policy WHERE site_id = %(s)s AND module = 'intent'", {"s": sid})
    devs = tx.fetchall("""SELECT id, serial, identity, model FROM device WHERE site_id = %(s)s
                          AND state IN ('adopted', 'offline')""", {"s": sid})
    invs = {str(d["id"]): inventory.load(tx, d["id"]) for d in devs}
    observed = [] if cur else dhcpguard.site_networks(devs, invs)["networks"]
    guess = None if cur else _guess_mgmt(observed, invs)
    if guess:
        # netagovaná síť se stejným rozsahem jako VLAN správy je táž síť viděná z brány – jako „VLAN 1“ by mátla
        observed = [o for o in observed if o.get("vlan_id") or o.get("subnet") != guess["subnet"]]
    it = cur["data"] if cur else intentmod.from_observed(observed)
    nets = {int(n["vlan_id"]): {"vid": int(n["vlan_id"]), "name": str(n.get("name") or f"VLAN {n['vlan_id']}"),
                                "purpose": n.get("purpose") or "lan", "subnet": n.get("subnet"),
                                "gateway": n.get("gateway"), "dns": n.get("dns") or []} for n in it.get("networks") or []}
    # VLAN, které na zařízeních jsou, ale nemají adresu (čistě L2) – ať jdou vybrat taky
    for inv in invs.values():
        for v in _rows(inv, "bridge_vlans"):
            for part in str(v.get("vlan-ids") or "").split(","):
                if part.isdigit() and 1 <= int(part) <= 4094 and int(part) not in nets:
                    nets[int(part)] = {"vid": int(part), "name": f"VLAN {part}", "purpose": "lan", "subnet": None,
                                       "gateway": None, "dns": []}
    mgmt = next((n["vid"] for n in nets.values() if n["purpose"] == "mgmt"), None)
    native = bool(it.get("mgmt_native", True))
    if guess and guess["vid"] in nets:
        for n in nets.values():
            n["purpose"] = "mgmt" if n["vid"] == guess["vid"] else ("lan" if n["purpose"] == "mgmt" else n["purpose"])
        mgmt, native = guess["vid"], guess["native"]
    profiles = tx.fetchall("SELECT id, name, pvid, tagged, poe, edge FROM port_profile ORDER BY name")
    return {"networks": sorted(nets.values(), key=lambda n: n["vid"]), "mgmt_vlan": mgmt,
            "mgmt_native": native, "managed": cur is not None, "stp": _site_stp(invs),
            "models": catalog(tx),
            "profiles": [{"id": p["id"], "name": p["name"], "pvid": p["pvid"],
                          "tagged": [int(x) for x in str(p["tagged"] or "").split(",") if x.strip().isdigit()],
                          # profil „všechny VLAN lokality“ (tagged = '*') odpovídá v průvodci trunku
                          "trunk": str(p["tagged"] or "").strip() == "*", "poe": p["poe"],
                          "edge": p["edge"]} for p in profiles]}


def _vid(v: Any, what: str) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ServiceError(f"{what}: neplatné číslo VLAN")
    if not 1 <= n <= 4094:
        raise ServiceError(f"{what}: VLAN musí být 1–4094")
    return n


def _ip(v: Any, what: str) -> str:
    try:
        return str(ipaddress.ip_address(str(v).strip()))
    except ValueError:
        raise ServiceError(f"{what}: neplatná IP adresa")


def validate(data: dict) -> dict:
    """Návrh z průvodce → normalizovaná podoba. Chyby = ServiceError s tím, co opravit."""
    name = str(data.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise ServiceError("jméno zařízení: 1–60 znaků, jen písmena bez diakritiky, číslice, tečka, pomlčka, podtržítko")
    model = str(data.get("model") or "").strip()[:80]
    if not model:
        raise ServiceError("chybí model zařízení")
    serial = str(data.get("serial") or "").strip().upper() or None
    if serial and not SERIAL_RE.match(serial):
        raise ServiceError("sériové číslo: 6–20 písmen a číslic (je na štítku zařízení)")
    names = data.get("port_names")
    if not isinstance(names, list) or not 1 <= len(names) <= 128:
        raise ServiceError("chybí seznam portů zařízení")
    names = [str(n) for n in names]
    if len(set(names)) != len(names) or not all(PORT_NAME.match(n) for n in names):
        raise ServiceError("seznam portů obsahuje neplatné nebo opakované jméno")
    poe_ports = [p for p in (data.get("poe_ports") or []) if p in names]
    extra = []
    for v in data.get("extra_vlans") or []:
        vid = _vid(v.get("vid") if isinstance(v, dict) else v, "další VLAN")
        extra.append({"vid": vid, "name": switchcfg.ascii_text((v.get("name") if isinstance(v, dict) else "") or f"VLAN {vid}", 40)})
    ports: dict[str, dict] = {}
    raw = data.get("ports") or {}
    if not isinstance(raw, dict):
        raise ServiceError("porty: čekám objekt port → nastavení")
    for port, c in raw.items():
        if port not in names or not isinstance(c, dict):
            raise ServiceError(f"port {port}: zařízení takový port nemá")
        mode = c.get("mode")
        if mode not in MODES:
            raise ServiceError(f"port {port}: neznámý režim")
        item: dict[str, Any] = {"mode": mode}
        if mode in ("access", "hybrid"):
            item["vlan"] = _vid(c.get("vlan"), f"port {port}")
        if mode == "hybrid":
            item["tagged"] = sorted({_vid(t, f"port {port}") for t in c.get("tagged") or []} - {item["vlan"]})
            if not item["tagged"]:
                item["mode"] = "access"
                item.pop("tagged")
        if mode != "off":
            item["edge"] = bool(c.get("edge", mode != "trunk"))
            poe = c.get("poe")
            if poe and poe not in POE:
                raise ServiceError(f"port {port}: neznámé nastavení PoE")
            if poe and port in poe_ports:
                item["poe"] = poe
            cm = switchcfg.ascii_text(c.get("comment") or "", 40)
            if cm:
                item["comment"] = cm
        ports[port] = item
    # LACP bondy: 2–8 portů, port nejvýš v jednom; členové mají společné nastavení (bere se z prvního)
    bonds: list[dict] = []
    used: set[str] = set()
    for b in data.get("bonds") or []:
        if not isinstance(b, dict):
            raise ServiceError("LACP: čekám seznam bondů")
        members = list({str(x) for x in b.get("ports") or []})
        if any(x not in names for x in members):
            raise ServiceError("LACP: bond obsahuje port, který zařízení nemá")
        members.sort(key=names.index)
        if not 2 <= len(members) <= 8:
            raise ServiceError("LACP: bond musí mít 2 až 8 portů")
        if used & set(members):
            raise ServiceError("LACP: port může být jen v jednom bondu")
        used |= set(members)
        cfg = ports.get(members[0])
        if not cfg or cfg["mode"] == "off":
            raise ServiceError("LACP: portům bondu nastav VLAN nebo trunk (vypnutý nebo nenastavený port v bondu být nemůže)")
        for x in members:
            keep_poe = (ports.get(x) or {}).get("poe")
            ports[x] = {k: v for k, v in cfg.items() if k != "poe"}
            if keep_poe:
                ports[x]["poe"] = keep_poe
        bonds.append({"name": f"bond{len(bonds) + 1}", "ports": members,
                      "lacp_rate": "1sec" if b.get("lacp_rate") == "1sec" else "30secs"})
    if len(bonds) > 16:
        raise ServiceError("LACP: nejvýš 16 bondů")
    m = data.get("mgmt") or {}
    mgmt: dict[str, Any] = {"vid": _vid(m.get("vid"), "VLAN správy"), "native": bool(m.get("native", True)),
                            "mode": "static" if m.get("mode") == "static" else "dhcp"}
    if mgmt["mode"] == "static":
        try:
            iface = ipaddress.ip_interface(str(m.get("address") or "").strip())
        except ValueError:
            raise ServiceError("správa: adresa musí být ve tvaru 10.0.0.5/24")
        if iface.network.prefixlen >= iface.max_prefixlen:
            raise ServiceError("správa: u adresy chybí maska (např. /24)")
        mgmt["address"] = str(iface)
        mgmt["gateway"] = _ip(m.get("gateway"), "správa: brána")
        if ipaddress.ip_address(mgmt["gateway"]) not in iface.network:
            raise ServiceError("správa: brána není ve stejné síti jako adresa")
        mgmt["dns"] = [_ip(x, "správa: DNS") for x in (m.get("dns") or [])][:3]
        if not mgmt["dns"]:
            raise ServiceError("správa: u pevné adresy zadej aspoň jeden DNS server (jinak se zařízení nespojí s controllerem)")
    st = data.get("stp") or {}
    stp: dict[str, Any] = {"mode": "mstp" if st.get("mode") == "mstp" else "rstp"}
    if stp["mode"] == "mstp":
        region = str(st.get("region") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]{0,32}", region):
            raise ServiceError("MSTP: jméno regionu smí mít nejvýš 32 znaků – písmena bez diakritiky, číslice, tečka, pomlčka, podtržítko")
        try:
            revision = int(st.get("revision") or 0)
        except (TypeError, ValueError):
            raise ServiceError("MSTP: revize regionu musí být číslo")
        if not 0 <= revision <= 65535:
            raise ServiceError("MSTP: revize regionu musí být 0–65535")
        stp.update(region=region, revision=revision)
    # IPv6 na switchi: vypnuto (výchozí – switch ho ke správě nepotřebuje), automaticky ze sítě (SLAAC), nebo ručně
    v6 = data.get("ipv6") or {}
    ipv6: dict[str, Any] = {"mode": v6.get("mode") if v6.get("mode") in ("auto", "static") else "off"}
    if ipv6["mode"] == "static":
        try:
            a6 = ipaddress.IPv6Interface(str(v6.get("address") or "").strip())
        except ValueError:
            raise ServiceError("IPv6: adresa musí být IPv6 s délkou prefixu, např. 2001:db8:93::20/64")
        if "/" not in str(v6.get("address")) or a6.ip.is_link_local or a6.ip.is_multicast or a6.ip.is_unspecified:
            raise ServiceError("IPv6: zadej adresu s délkou prefixu (např. 2001:db8:93::20/64), ne link-local")
        ipv6["address"] = a6.compressed
        gw6 = str(v6.get("gateway") or "").strip()
        if gw6:
            try:
                g6 = ipaddress.IPv6Address(gw6)
            except ValueError:
                raise ServiceError("IPv6: brána musí být IPv6 adresa")
            if g6.is_multicast or g6.is_unspecified:
                raise ServiceError("IPv6: brána musí být adresa routeru")
            ipv6["gateway"] = g6.compressed
        else:
            ipv6["gateway"] = ""
    unset = "default" if data.get("unset") == "default" else "off"
    out = {"name": name, "model": model, "serial": serial, "port_names": names, "poe_ports": poe_ports, "ports": ports,
           "mgmt": mgmt, "unset": unset, "extra_vlans": extra, "stp": stp, "bonds": bonds, "ipv6": ipv6}
    if unset == "default":
        out["unset_vlan"] = _vid(data.get("unset_vlan"), "VLAN pro nenastavené porty")
    return out


def build(plan: dict, ctx: dict) -> dict:
    """Model pro `switchcfg.config_body` + co brání vygenerování (`blockers`) a na co upozornit (`warnings`)."""
    mg, native = plan["mgmt"]["vid"], plan["mgmt"]["native"]
    names = {n["vid"]: n["name"] for n in ctx["networks"]}
    names.update({v["vid"]: v["name"] for v in plan["extra_vlans"]})
    blockers: list[str] = []
    warnings: list[str] = []
    if mg not in names:
        blockers.append(f"VLAN správy {mg} není mezi sítěmi lokality")
    items: list[dict] = []
    extra_lines: list[str] = []
    # LACP: členy nahradí v bridgi jeden port – bond (na místě svého prvního člena), nastavení mají společné
    bond_of = {x: b for b in plan.get("bonds") or [] for x in b["ports"]}
    for port in plan["port_names"]:
        c = plan["ports"].get(port)
        b = bond_of.get(port)
        if b and port != b["ports"][0]:
            if c and c.get("poe") and c["poe"] != "auto-on":
                extra_lines.append(f":do {{ /interface ethernet poe set [find default-name={q(port)}] poe-out={c['poe']} }} on-error={{}}")
            continue
        phys = port
        if b:
            port = b["name"]
        if c is None:
            c = {"mode": "access", "vlan": plan["unset_vlan"], "edge": True} if plan["unset"] == "default" else {"mode": "off"}
        mode = c["mode"]
        it: dict[str, Any] = {"port": port, "name": port, "mode": mode, "comment": c.get("comment"), "trusted": mode == "trunk"}
        if b:
            it["bond"] = {"slaves": b["ports"], "lacp_rate": b["lacp_rate"], "hash": "layer-2-and-3"}
        if mode == "off":
            it.update(pvid=None, frame_types=None, loop=False)
        elif mode == "trunk":
            it.update(pvid=mg if native else 1, frame_types=switchcfg.FT_ALL if native else switchcfg.FT_TRUNK_TAGGED, loop=False)
        else:
            for vid in [c["vlan"], *c.get("tagged", [])]:
                if vid not in names:
                    blockers.append(f"port {port}: VLAN {vid} není mezi sítěmi lokality (přidej ji v kroku Porty)")
            it.update(pvid=c["vlan"], tagged=c.get("tagged", []), loop=not b,
                      frame_types=switchcfg.FT_ACCESS if mode == "access" else switchcfg.FT_ALL)
        items.append(it)
        if mode != "off":
            # config_body dává edge=yes jen přístupovým portům; zbytek doladit podle návrhu
            want = "yes" if c.get("edge") else "auto"
            if want != ("yes" if mode == "access" else "auto"):
                extra_lines.append(f"/interface bridge port set [find bridge={switchcfg.BRIDGE} interface={q(port)}] edge={want}")
            if c.get("poe") and c["poe"] != "auto-on":
                extra_lines.append(f":do {{ /interface ethernet poe set [find default-name={q(phys)}] poe-out={c['poe']} }} on-error={{}}")
    for b in plan.get("bonds") or []:
        warnings.append(f"LACP {b['name']} ({', '.join(b['ports'])}): protistrana musí mít LACP (802.3ad) taky, jinak linka nepojede; "
                        "pro Hyper-V SET bond nepoužívej (SET LACP neumí)")
    vlans = []
    for vid in sorted(names):
        tagged, untagged = [], []
        if vid == mg:
            # správa je na VLAN rozhraní nad bridgem → bridge (CPU port) je v té VLAN tagovaný
            tagged.append(switchcfg.BRIDGE)
        for it in items:
            if it["mode"] == "trunk":
                (untagged if native and vid == mg else tagged).append(it["port"])
            elif it["mode"] in ("access", "hybrid") and it["pvid"] == vid:
                untagged.append(it["port"])
            elif it["mode"] == "hybrid" and vid in it["tagged"]:
                tagged.append(it["port"])
        vlans.append({"vid": vid, "name": names[vid], "tagged": tagged, "untagged": untagged})
    carriers = [it["port"] for it in items if it["mode"] == "trunk"
                or (it["mode"] in ("access", "hybrid") and it["pvid"] == mg)
                or (it["mode"] == "hybrid" and mg in it["tagged"])]
    if not carriers:
        blockers.append(f"VLAN správy {mg} nevede ven žádným portem – zařízení by se po zapojení neohlásilo "
                        "(nastav trunk nebo port ve VLAN správy)")
    if not any(it["mode"] == "trunk" for it in items):
        warnings.append("žádný port není trunk – ostatní VLAN se ze switche nedostanou dál")
    n_off = sum(1 for it in items if it["mode"] == "off")
    if n_off and plan["unset"] == "off":
        warnings.append(f"{n_off} portů zůstane vypnutých (nenastavené porty)")
    stp = plan.get("stp") or {"mode": "rstp"}
    if stp["mode"] == "mstp":
        # všechny VLAN zůstávají v CIST (instance 0); mapování VLAN do dalších instancí se nepřenáší
        extra_lines.append(f"/interface bridge set {switchcfg.BRIDGE} protocol-mode=mstp region-name={q(stp.get('region') or '')} "
                           f"region-revision={int(stp.get('revision') or 0)}")
        warnings.append("MSTP: nastaví se jméno a revize regionu, všechny VLAN zůstanou v základní instanci (CIST) – "
                        "pokud síť mapuje VLAN do dalších instancí (MSTI), doplň je po adopci, jinak switch zůstane mimo region")
        if not stp.get("region"):
            warnings.append("MSTP: jméno regionu je prázdné – musí být stejné jako na ostatních switchích")
    # IPv6: nastavení jde přes on-error – selhání nesmí shodit zbytek skriptu (ohlášení controlleru)
    v6 = plan.get("ipv6") or {"mode": "off"}
    mif = switchcfg.mgmt_iface(mg)
    if v6["mode"] == "off":
        v6_cmds = ["/ipv6 settings set disable-ipv6=yes"]
    elif v6["mode"] == "auto":
        # switch je koncové zařízení: neroutuje a adresu si bere z ohlášení routeru (SLAAC)
        v6_cmds = ["/ipv6 settings set disable-ipv6=no forward=no accept-router-advertisements=yes accept-redirects=no"]
    else:
        v6_cmds = ["/ipv6 settings set disable-ipv6=no forward=no accept-router-advertisements=no accept-redirects=no",
                   f"/ipv6 address add address={q(v6['address'])} interface={q(mif)} advertise=no comment=\"mtc\""]
        if v6.get("gateway"):
            gw6 = v6["gateway"] + (f"%{mif}" if ipaddress.IPv6Address(v6["gateway"]).is_link_local else "")
            v6_cmds.append(f"/ipv6 route add dst-address=::/0 gateway={q(gw6)} comment=\"mtc mgmt\"")
    extra_lines.append(":do { " + "; ".join(v6_cmds) + " } on-error={ :log warning \"mtc: nastaveni IPv6 selhalo\" }")
    if v6["mode"] == "auto":
        warnings.append("IPv6 automaticky: switch si vezme adresu z ohlášení routeru ve VLAN správy – na síti musí být router, který je posílá")
    if plan["mgmt"]["mode"] == "dhcp":
        warnings.append("správa přes DHCP – ve VLAN správy musí běžet DHCP server; doporučuji rezervaci adresy")
    m = plan["mgmt"]
    mgmt = {"vid": mg, "native": native, "mode": m["mode"], "style": "vlan"}
    if m["mode"] == "static":
        mgmt.update(address=m["address"], gateway=m["gateway"], dns=m["dns"])
    return {"bridge": switchcfg.BRIDGE, "ports": items, "vlans": vlans, "mgmt": mgmt, "snooping": False,
            "identity": plan["name"], "timezone": regional.timezone(),
            "ntp": [h for h in regional.ntp_servers() if re.fullmatch(r"[A-Za-z0-9.-]{1,100}", h)],
            "dns_servers": None, "isolate": None, "gw": None, "cap": None,
            "blockers": blockers, "warnings": warnings, "extra_lines": extra_lines, "carriers": carriers}


def script(plan: dict, ctx: dict, enroll_url: str, opts: FetchOpts) -> str:
    """Skript pro čisté zařízení (reset bez výchozí konfigurace): naše nastavení + ohlášení controlleru."""
    model = build(plan, ctx)
    if model["blockers"]:
        raise ServiceError("; ".join(model["blockers"]))
    enroll = defaults.enroll_source(enroll_url, opts)
    lines = [
        switchcfg.config_body(model).rstrip(),
        *model["extra_lines"],
        # značka role: controller po adopci pozná, že konfigurace je naše a zařízení může řídit bez resetu
        f"/interface bridge set {switchcfg.BRIDGE} comment={q(defaults.MARK + 'switch')}",
        "/tool mac-server mac-winbox set allowed-interface-list=all",
        "/ip neighbor discovery-settings set discover-interface-list=all",
        "# --- ohlaseni controlleru (do adopce) ---",
        '/system scheduler remove [find name="mtc-enroll"]',
        '/system script remove [find name="mtc-enroll"]',
        f'/system script add name="mtc-enroll" policy={SCRIPT_POLICY} comment="mtc" source={q_source(enroll)}',
        f'/system scheduler add name="mtc-enroll" interval=1m start-date=[/system clock get date] start-time=00:00:00 '
        f'policy={SCRIPT_POLICY} comment="mtc" on-event="/system script run mtc-enroll"',
    ]
    return (f"# MikroTik controller - pripravena konfigurace {plan['name']} ({switchcfg.ascii_text(plan['model'], 40)})\n"
            "# Plati pro ciste zarizeni: /system reset-configuration no-defaults=yes run-after-reset=<tento soubor>\n"
            "{\n" + defaults._wait_ports() + "\n".join(lines)
            + '\n:log warning "mtc: pripravena konfigurace nastavena, ohlasuji se controlleru"\n}\n')


def summary(plan: dict, model: dict) -> list[dict]:
    """Skupiny portů se stejným nastavením (pro kontrolu a štítek): [{ports: [...], text}]."""
    groups: dict[tuple, list[str]] = {}
    for it in model["ports"]:
        members = it["bond"]["slaves"] if it.get("bond") else [it["port"]]
        bond = it["port"] if it.get("bond") else None
        c = plan["ports"].get(members[0]) or {}
        if it["mode"] == "off":
            key: tuple = ("off", None, None, None, None, None, None)
        elif it["mode"] == "trunk":
            key = ("trunk", bool(c.get("edge")), c.get("poe"), c.get("comment"), None, None, bond)
        else:
            key = (it["mode"], it["pvid"], tuple(it.get("tagged") or []), bool(c.get("edge", True)), c.get("poe"), c.get("comment"), bond)
        groups.setdefault(key, []).extend(members)
    return [{"ports": ports, "mode": key[0], "bond": key[6], "vlan": key[1] if key[0] in ("access", "hybrid") else None,
             "tagged": list(key[2]) if key[0] in ("access", "hybrid") else [],
             "edge": key[3] if key[0] in ("access", "hybrid") else (key[1] if key[0] == "trunk" else False),
             "poe": key[4] if key[0] in ("access", "hybrid") else (key[2] if key[0] == "trunk" else None),
             "comment": key[5] if key[0] in ("access", "hybrid") else (key[3] if key[0] == "trunk" else None)}
            for key, ports in groups.items()]


# --- uložení -----------------------------------------------------------------------------------------

def _row(r: dict) -> dict:
    # fáze nasazení: návrh → hotový → skript stažen → zařízení se ohlásilo → schváleno
    if r["status"] == "draft":
        stage = "draft"
    elif r["device_id"]:
        stage = "adopted" if r.get("device_state") in ("adopted", "offline") else "checked_in"
    else:
        stage = "fetched" if r.get("fetched_at") else "ready"
    return {"id": r["id"], "name": r["name"], "model": r["model"], "serial": r["serial"], "status": r["status"],
            "device_id": str(r["device_id"]) if r["device_id"] else None, "data": r["data"], "stage": stage,
            "fetched_at": r.get("fetched_at"), "created_at": r["created_at"], "updated_at": r["updated_at"]}


_PLAN_SQL = """SELECT p.id, p.name, p.model, p.serial, p.status, p.device_id, p.data, p.created_at, p.updated_at, p.fetched_at,
                      d.state::text AS device_state
               FROM device_prep p LEFT JOIN device d ON d.id = p.device_id WHERE p.site_id = %(s)s"""


def list_plans(tx: Tx, site_id: Any) -> list[dict]:
    return [_row(r) for r in tx.fetchall(_PLAN_SQL + " ORDER BY (p.status = 'deployed'), p.name", {"s": str(site_id)})]


def get_plan(tx: Tx, site_id: Any, pid: Any) -> dict:
    r = tx.fetchone(_PLAN_SQL + " AND p.id = %(p)s", {"s": str(site_id), "p": int(pid)})
    if not r:
        raise NotFound("připravené zařízení neexistuje")
    return _row(r)


def mark_fetched(tx: Tx, site_id: Any, pid: Any) -> None:
    """Skript opustil controller (soubor stažen nebo vyzvednut zařízením) – jen informace pro přehled fází."""
    tx.execute("UPDATE device_prep SET fetched_at = now() WHERE site_id = %(s)s AND id = %(p)s AND device_id IS NULL",
               {"s": str(site_id), "p": int(pid)})


def compare(plan: dict, ctx: dict, inv: dict, identity: str | None = None) -> dict:
    """Návrh × skutečnost po adopci (z inventury): jméno, vypnuté porty, PVID a tagované VLAN portů.
    Vrací rozdíly; prázdný seznam = zařízení odpovídá návrhu. Bez inventury bridge portů nejde porovnat."""
    model = build(plan, ctx)
    bports = {str(b.get("interface")): b for b in _rows(inv, "bridge_ports") if str(b.get("disabled")).lower() != "true"}
    if not bports:
        return {"known": False, "diffs": [], "checked": 0}
    eth = {str(e.get("default-name") or e.get("name")): e for e in _rows(inv, "ethernet")}
    have_tag: dict[str, set[int]] = {}
    for v in _rows(inv, "bridge_vlans"):
        if str(v.get("disabled")).lower() == "true" or str(v.get("dynamic")).lower() == "true":
            continue
        for port in [x.strip() for x in str(v.get("tagged") or "").split(",") if x.strip()]:
            have_tag.setdefault(port, set()).update(dhcpguard._vids(v.get("vlan-ids")))
    want_tag: dict[str, set[int]] = {}
    for v in model["vlans"]:
        for port in v["tagged"]:
            want_tag.setdefault(port, set()).add(int(v["vid"]))
    diffs: list[dict] = []

    def diff(port: str, what: str, want: Any, have: Any) -> None:
        diffs.append({"port": port, "what": what, "want": want, "have": have})

    if identity and identity != plan["name"]:
        diff("", "identity", plan["name"], identity)
    inv_bonds = {str(b.get("name")): sorted(x.strip() for x in str(b.get("slaves") or "").split(",") if x.strip()) for b in _rows(inv, "bonds")}
    for it in model["ports"]:
        port = it["port"]
        if it.get("bond") and inv_bonds.get(port) != sorted(it["bond"]["slaves"]):
            diff(port, "bond", it["bond"]["slaves"], inv_bonds.get(port) or [])
            continue
        e = eth.get(port) or {}
        name = str(e.get("name") or port)          # port mohl být po adopci přejmenován
        off = str(e.get("disabled")).lower() == "true"
        if it["mode"] == "off":
            if e and not off:
                diff(port, "disabled", True, False)
            continue
        if off:
            diff(port, "disabled", False, True)
            continue
        bp = bports.get(name)
        if not bp:
            diff(port, "bridge", True, False)
            continue
        native = model["mgmt"]["native"]
        if (it["mode"] != "trunk" or native) and str(bp.get("pvid") or "1") != str(it["pvid"]):
            diff(port, "pvid", it["pvid"], int(bp["pvid"]) if str(bp.get("pvid") or "").isdigit() else None)
        want, have = want_tag.get(port, set()), have_tag.get(name, set())
        if want != have:
            diff(port, "tagged", sorted(want), sorted(have))
    return {"known": True, "diffs": diffs, "checked": len(model["ports"])}


def save(db: Database, tenant_id: Any, site_id: Any, data: dict, pid: int | None = None, user_id: Any = None,
         ip: str | None = None) -> dict:
    plan = validate(data)
    with tenant_scope(db, tenant_id) as tx:
        if not tx.fetchone("SELECT 1 FROM site WHERE id = %(s)s", {"s": str(site_id)}):
            raise NotFound("lokalita neexistuje")
        ctx = context(tx, site_id)
        model = build(plan, ctx)
        status = "draft" if model["blockers"] else "ready"
        clash = tx.fetchone("SELECT id FROM device_prep WHERE site_id = %(s)s AND name = %(n)s", {"s": str(site_id), "n": plan["name"]})
        if clash and clash["id"] != pid:
            raise ServiceError(f"připravené zařízení se jménem {plan['name']} už v lokalitě je")
        p = {"t": str(tenant_id), "s": str(site_id), "n": plan["name"], "m": plan["model"], "sn": plan["serial"],
             "d": plan, "st": status, "u": str(user_id) if user_id else None}
        if pid is None:
            row = tx.fetchone("""INSERT INTO device_prep (tenant_id, site_id, name, model, serial, data, status, created_by)
                                 VALUES (%(t)s, %(s)s, %(n)s, %(m)s, %(sn)s, %(d)s, %(st)s, %(u)s) RETURNING id""", p)
            pid = row["id"]
            audit(tx, "prep.create", tenant_id=tenant_id, user_id=user_id, target=f"prep:{pid}",
                  after={"name": plan["name"], "model": plan["model"], "status": status}, ip=ip)
        else:
            old = tx.fetchone("SELECT status, name FROM device_prep WHERE site_id = %(s)s AND id = %(p)s FOR UPDATE",
                              {"s": str(site_id), "p": int(pid)})
            if not old:
                raise NotFound("připravené zařízení neexistuje")
            if old["status"] == "deployed":
                raise ServiceError("zařízení už je nasazené – porty teď měň v jeho detailu")
            tx.execute("""UPDATE device_prep SET name = %(n)s, model = %(m)s, serial = %(sn)s, data = %(d)s, status = %(st)s,
                              updated_at = now() WHERE id = %(p)s""", dict(p, p=int(pid)))
            audit(tx, "prep.update", tenant_id=tenant_id, user_id=user_id, target=f"prep:{pid}",
                  before={"name": old["name"], "status": old["status"]}, after={"name": plan["name"], "status": status}, ip=ip)
        out = get_plan(tx, site_id, pid)
    return {**out, "blockers": model["blockers"], "warnings": model["warnings"]}


def delete(db: Database, tenant_id: Any, site_id: Any, pid: int, user_id: Any = None, ip: str | None = None) -> None:
    with tenant_scope(db, tenant_id) as tx:
        r = tx.fetchone("DELETE FROM device_prep WHERE site_id = %(s)s AND id = %(p)s RETURNING name",
                        {"s": str(site_id), "p": int(pid)})
        if not r:
            raise NotFound("připravené zařízení neexistuje")
        audit(tx, "prep.delete", tenant_id=tenant_id, user_id=user_id, target=f"prep:{pid}", before={"name": r["name"]}, ip=ip)


def review(tx: Tx, site_id: Any, pid: int, enroll_url: str, opts: FetchOpts) -> dict:
    """Kontrola a skript: souhrn portů, co brání, upozornění a text skriptu (bez něj, když něco brání)."""
    p = get_plan(tx, site_id, pid)
    ctx = context(tx, site_id)
    plan = validate(p["data"])
    model = build(plan, ctx)
    names = {n["vid"]: n["name"] for n in ctx["networks"]}
    names.update({v["vid"]: v["name"] for v in plan["extra_vlans"]})
    return {"plan": p, "blockers": model["blockers"], "warnings": model["warnings"], "summary": summary(plan, model),
            "carriers": model["carriers"], "vlan_names": {str(k): v for k, v in names.items()},
            "script": None if model["blockers"] else script(plan, ctx, enroll_url, opts)}


def script_for_key(db: Database, key: str, pid: int, enroll_url: str, opts: FetchOpts) -> str:
    """Skript pro jednořádkový příkaz na zařízení: ověří klíč lokality a návrh hledá jen v té lokalitě."""
    if not key or len(key) > 100:
        raise NotFound("klíč lokality je neplatný")
    with operator_scope(db) as tx:
        s = tx.fetchone("SELECT site_id, tenant_id FROM site_policy WHERE module = 'enroll' AND data->>'key_hash' = %(h)s",
                        {"h": token_hash(key)})
    if not s:
        raise NotFound("klíč lokality je neplatný")
    with tenant_scope(db, s["tenant_id"]) as tx:
        p = get_plan(tx, s["site_id"], pid)
        ctx = context(tx, s["site_id"])
        out = script(validate(p["data"]), ctx, enroll_url, opts)
        mark_fetched(tx, s["site_id"], pid)
    return out


def pair(tx: Tx, site_id: Any, device_id: Any, serial: str | None, model: str | None) -> dict | None:
    """Ohlášené zařízení ↔ návrh: podle sériového čísla, jinak jediný volný hotový návrh téhož modelu v lokalitě."""
    if not site_id:
        return None
    sid = str(site_id)
    r = None
    if serial:
        r = tx.fetchone("""SELECT id, name FROM device_prep WHERE site_id = %(s)s AND device_id IS NULL AND status = 'ready'
                           AND serial = %(sn)s""", {"s": sid, "sn": str(serial).upper()})
    if not r and model:
        cand = [x for x in tx.fetchall("""SELECT id, name, model FROM device_prep WHERE site_id = %(s)s AND device_id IS NULL
                                          AND status = 'ready' AND serial IS NULL""", {"s": sid})
                if model_key(x["model"]) == model_key(model)]
        r = cand[0] if len(cand) == 1 else None
    if not r:
        return None
    tx.execute("UPDATE device_prep SET device_id = %(d)s, status = 'deployed', updated_at = now() WHERE id = %(p)s",
               {"d": str(device_id), "p": r["id"]})
    return {"id": r["id"], "name": r["name"]}
