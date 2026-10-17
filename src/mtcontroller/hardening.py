"""Bezpečnostní audit konfigurace zařízení (jen čtení, z inventury).

Kontroly podle doporučení MikroTiku pro zabezpečení routeru. Každý nález má
závažnost, krátký popis a návrh opravy (příkaz RouterOS). Controller nic
nemění – oprava je na správci (později jako plán změn s rollbackem).

Kritické nálezy (veřejná IP bez filtru na input, otevřený DNS resolver) vedou
na alert `security_exposed`.
"""

from __future__ import annotations

import ipaddress
from typing import Any

# služby bez šifrování
_PLAINTEXT = {"telnet": "Telnet", "ftp": "FTP", "www": "WebFig přes HTTP", "api": "API (8728) bez TLS"}
_ENCRYPTED = {"ssh", "winbox", "api-ssl", "www-ssl"}


def _rows(inv: dict, key: str) -> list[dict]:
    v = inv.get(key)
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def _obj(inv: dict, key: str) -> dict:
    v = inv.get(key)
    return v if isinstance(v, dict) else {}


def _true(v: Any) -> bool:
    return str(v).lower() in ("true", "yes")


def public_addresses(inv: dict) -> list[str]:
    out = []
    for a in _rows(inv, "addresses"):
        if _true(a.get("disabled")):
            continue
        try:
            ip = ipaddress.ip_interface(str(a.get("address"))).ip
        except ValueError:
            continue
        if ip.version == 4 and ip.is_global:
            out.append(f"{ip} ({a.get('interface')})")
    return out


def input_catch_all(inv: dict) -> bool:
    """Má input chain pravidlo, které zahazuje „vše ostatní“ (případně z WAN rozhraní)?
    Pravidla omezená na protokol, port, zdrojovou adresu nebo jen 'invalid' se nepočítají."""
    for r in _rows(inv, "fw_filter"):
        if r.get("chain") != "input" or _true(r.get("disabled")) or _true(r.get("dynamic")):
            continue
        if r.get("action") not in ("drop", "reject", "tarpit"):
            continue
        if any(r.get(k) for k in ("protocol", "dst-port", "src-address", "src-address-list")):
            continue
        if r.get("connection-state") and set(str(r["connection-state"]).split(",")) <= {"invalid"}:
            continue
        return True
    return False


def audit(inv: dict) -> list[dict]:
    if not inv:
        return []
    out: list[dict] = []

    def add(sev: str, code: str, text: str, fix: str = "") -> None:
        out.append({"severity": sev, "code": code, "text": text, "fix": fix})

    pub = public_addresses(inv)
    guarded = input_catch_all(inv)
    if pub and "fw_filter" in inv and not guarded:
        add("critical", "no_input_firewall",
            f"veřejná IP {', '.join(pub)} a input chain nic nezahazuje – router je otevřený z internetu",
            '/ip firewall filter add chain=input in-interface-list=!LAN action=drop comment="drop all not from LAN"')
    dns = _obj(inv, "dns")
    if _true(dns.get("allow-remote-requests")) and pub and not guarded and "fw_filter" in inv:
        add("critical", "open_resolver", "DNS odpovídá z internetu (otevřený resolver – zneužití k DDoS)",
            "/ip dns set allow-remote-requests=no  nebo firewall na input udp/tcp 53 z WAN")

    for s in _rows(inv, "ip_services"):
        name = str(s.get("name") or "")
        if _true(s.get("disabled")) or name.startswith("mtc"):
            continue
        restricted = bool(str(s.get("address") or "").strip())
        if name in _PLAINTEXT:
            add("warning" if not restricted else "info", f"service_{name}",
                f"{_PLAINTEXT[name]} je zapnuté{'' if restricted else ' a dostupné odkudkoli'} – nešifrovaný provoz",
                f"/ip service set {name} disabled=yes")
        elif name in _ENCRYPTED and not restricted and pub:
            add("info", f"service_{name}_open", f"{name} není omezené na adresy správy",
                f"/ip service set {name} address=<síť správy>")

    for u in _rows(inv, "users"):
        if str(u.get("name")) == "admin" and not _true(u.get("disabled")):
            add("info", "default_admin", "výchozí uživatel „admin“ existuje – cíl útoků hrubou silou",
                "vytvoř vlastní účet ve skupině full, pak /user disable admin")
            break

    for key, label in (("mac_server", "MAC-Telnet"), ("mac_winbox", "MAC-Winbox")):
        if str(_obj(inv, key).get("allowed-interface-list")) == "all":
            add("warning", f"{key}_all", f"{label} povolený na všech rozhraních (i WAN)",
                f"/tool mac-server{' mac-winbox' if key == 'mac_winbox' else ''} set allowed-interface-list=LAN")
    if str(_obj(inv, "neighbor_discovery").get("discover-interface-list")) == "all":
        add("info", "discovery_all", "neighbor discovery (MNDP/LLDP/CDP) na všech rozhraních – prozrazuje model a verzi",
            "/ip neighbor discovery-settings set discover-interface-list=LAN")

    if _true(_obj(inv, "snmp").get("enabled")):
        for c in _rows(inv, "snmp_communities"):
            if c.get("is_public") and not _true(c.get("disabled")):
                add("warning", "snmp_public",
                    "SNMP s výchozí komunitou „public“" + (" dostupnou odkudkoli" if c.get("open") else ""),
                    "/snmp community set public name=<tajné jméno> addresses=<monitoring>")
                break
    for key, label in (("socks", "SOCKS proxy"), ("proxy", "HTTP proxy")):
        if _true(_obj(inv, key).get("enabled")):
            add("warning", f"{key}_enabled", f"{label} je zapnutá – častý znak kompromitace", f"/ip {key} set enabled=no")
    if _true(_obj(inv, "bw_server").get("enabled")):
        add("info", "bw_server", "bandwidth-test server je zapnutý", "/tool bandwidth-server set enabled=no")
    if _true(_obj(inv, "romon").get("enabled")):
        add("info", "romon", "RoMON je zapnutý", "/tool romon set enabled=no (pokud ho nepoužíváš)")

    order = {"critical": 0, "warning": 1, "info": 2}
    out.sort(key=lambda f: order[f["severity"]])
    return out
