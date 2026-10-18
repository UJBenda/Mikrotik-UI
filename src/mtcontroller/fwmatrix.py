"""Zónový firewall („malý Fortinet“) pro RouterOS v7.

- zóna = /interface list (WAN, LAN, později HOSTE, IOT…),
- objekt = /ip firewall address-list (např. mtc-admin = kdo smí spravovat router),
- politika = dvojice zón zdroj → cíl s akcí accept/drop; na konci výchozí zákaz.

Před maticí obecná pravidla podle defconf MikroTiku (navázaná spojení, neplatné
pakety, ICMP, WAN bez dstnat). Pravidla mají komentář `mtc-fw: …` – pohled
„Matice zón“ v UI (firewall.py) je čte stejně jako ruční konfiguraci.
Pozor: prefix `mtc:` patří pravidlům tunelu (provisioning), proto `mtc-fw:`.
"""

from __future__ import annotations

import ipaddress
import re

from .routeros import q

ZONE_RE = re.compile(r"^[A-Z][A-Z0-9_-]{0,15}$")
IFACE_RE = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
ACTIONS = ("accept", "drop")
ADMIN_LIST = "mtc-admin"
# služby routeru pro vnitřní zóny: DNS, DHCP, NTP
SERVICES = (("udp", "53,67,123"), ("tcp", "53"))


class MatrixError(ValueError):
    pass


def validate(zones: dict[str, list[str]], policies: list[dict], admin: list[str], wan: str = "WAN") -> None:
    if wan not in zones:
        raise MatrixError(f"chybí zóna {wan}")
    for z, members in zones.items():
        if not ZONE_RE.match(z):
            raise MatrixError(f"neplatné jméno zóny {z!r} (velká písmena, číslice, max 16)")
        for m in members:
            if not IFACE_RE.match(m):
                raise MatrixError(f"neplatné rozhraní {m!r} v zóně {z}")
    seen = set()
    for p in policies:
        if p.get("src") not in zones or p.get("dst") not in zones:
            raise MatrixError(f"politika {p.get('src')} → {p.get('dst')}: neznámá zóna")
        if p.get("action") not in ACTIONS:
            raise MatrixError(f"politika {p['src']} → {p['dst']}: akce accept nebo drop")
        if (p["src"], p["dst"]) in seen:
            raise MatrixError(f"politika {p['src']} → {p['dst']} je dvakrát")
        seen.add((p["src"], p["dst"]))
    for a in admin:
        try:
            ipaddress.ip_network(a, strict=False)
        except ValueError:
            raise MatrixError(f"neplatná adresa správy {a!r}") from None


def render(zones: dict[str, list[str]], policies: list[dict], admin: list[str], wan: str = "WAN",
           intra_allow: bool = True) -> str:
    """Celý firewall pro čistou konfiguraci (výchozí konfigurace role firewall).

    zones: {"WAN": ["ether1"], "LAN": ["bridge"]}; policies: [{"src", "dst", "action"}];
    admin: sítě, odkud smí správa (Winbox, SSH, WebFig); intra_allow: provoz uvnitř zóny povolen."""
    validate(zones, policies, admin, wan)
    inner = [z for z in zones if z != wan]
    L: list[str] = ["# --- zony (interface listy) ---"]
    for z, members in zones.items():
        L.append(f"/interface list add name={q(z)} comment=\"mtc-fw zona\"")
        for m in members:
            L.append(f"/interface list member add list={q(z)} interface={q(m)} comment=\"mtc-fw\"")
    L.append("# --- objekty (address listy) ---")
    for a in admin:
        L.append(f"/ip firewall address-list add list={ADMIN_LIST} address={q(a)} comment=\"mtc-fw: sprava routeru\"")
    f = "/ip firewall filter add"
    L += ["# --- input: router sam ---",
          f'{f} chain=input action=accept connection-state=established,related,untracked comment="mtc-fw: input navazana"',
          f'{f} chain=input action=drop connection-state=invalid comment="mtc-fw: input neplatne"',
          f'{f} chain=input action=accept protocol=icmp comment="mtc-fw: input ICMP"',
          f'{f} chain=input action=accept dst-address=127.0.0.1 comment="mtc-fw: input loopback (CAPsMAN)"',
          f'{f} chain=input action=accept src-address-list={ADMIN_LIST} in-interface-list=!{wan} '
          f'comment="mtc-fw: input sprava z {ADMIN_LIST}"']
    for z in inner:
        for proto, ports in SERVICES:
            L.append(f'{f} chain=input action=accept in-interface-list={q(z)} protocol={proto} dst-port={ports} '
                     f'comment="mtc-fw: input sluzby {z}"')
    L.append(f'{f} chain=input action=drop comment="mtc-fw: input vychozi zakaz"')
    L += ["# --- forward: matice zon ---",
          f'{f} chain=forward action=fasttrack-connection hw-offload=yes connection-state=established,related '
          f'comment="mtc-fw: fasttrack"',
          f'{f} chain=forward action=accept connection-state=established,related,untracked comment="mtc-fw: navazana"',
          f'{f} chain=forward action=drop connection-state=invalid comment="mtc-fw: neplatne"',
          f'{f} chain=forward action=drop in-interface-list={wan} connection-nat-state=!dstnat connection-state=new '
          f'comment="mtc-fw: {wan} bez dstnat"']
    for p in policies:
        L.append(f'{f} chain=forward action={p["action"]} in-interface-list={q(p["src"])} out-interface-list={q(p["dst"])} '
                 f'comment="mtc-fw: {p["src"]} -> {p["dst"]}"')
    if intra_allow:
        for z in inner:
            L.append(f'{f} chain=forward action=accept in-interface-list={q(z)} out-interface-list={q(z)} '
                     f'comment="mtc-fw: {z} -> {z}"')
    L.append(f'{f} chain=forward action=drop comment="mtc-fw: forward vychozi zakaz"')
    L += ["# --- NAT ---",
          f'/ip firewall nat add chain=srcnat action=masquerade out-interface-list={wan} ipsec-policy=out,none '
          f'comment="mtc-fw: maskarada {wan}"',
          "# --- IPv6: jen odchozi, z WAN nic ---"]
    g = "/ipv6 firewall filter add"
    L += [f'{g} chain=input action=accept connection-state=established,related,untracked comment="mtc-fw: v6 navazana"',
          f'{g} chain=input action=drop connection-state=invalid comment="mtc-fw: v6 neplatne"',
          f'{g} chain=input action=accept protocol=icmpv6 comment="mtc-fw: v6 ICMP"',
          f'{g} chain=input action=accept protocol=udp dst-port=546 src-address=fe80::/10 comment="mtc-fw: v6 DHCP klient"',
          f'{g} chain=input action=drop in-interface-list={wan} comment="mtc-fw: v6 input z {wan}"',
          f'{g} chain=forward action=accept connection-state=established,related,untracked comment="mtc-fw: v6 navazana"',
          f'{g} chain=forward action=drop connection-state=invalid comment="mtc-fw: v6 neplatne"',
          f'{g} chain=forward action=accept protocol=icmpv6 comment="mtc-fw: v6 ICMP"',
          f'{g} chain=forward action=drop in-interface-list={wan} comment="mtc-fw: v6 z {wan}"']
    return "\n".join(L) + "\n"


def matrix(zones: dict[str, list[str]], policies: list[dict], wan: str = "WAN", intra_allow: bool = True) -> dict:
    """Matice pro zobrazení: {zdroj: {cíl: accept|drop}} včetně výchozího zákazu."""
    pol = {(p["src"], p["dst"]): p["action"] for p in policies}
    out: dict[str, dict[str, str]] = {}
    for s in zones:
        out[s] = {}
        for d in zones:
            if (s, d) in pol:
                out[s][d] = pol[(s, d)]
            elif s == d and s != wan and intra_allow:
                out[s][d] = "accept"
            else:
                out[s][d] = "drop"
    return out
