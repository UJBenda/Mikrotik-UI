"""Výchozí konfigurace podle role (jako tovární nastavení UniFi) a ohlášení controlleru.

Technik zvolí roli, controller vygeneruje skript `mtc-default.rsc` a ten jde na
zařízení buď Netinstallem jako vlastní výchozí konfigurace (`netinstall -s`,
platí i po resetu tlačítkem), nebo na běžícím kusu přes
`reset-configuration no-defaults=yes run-after-reset=`.

Výchozí stav (všechny role kromě firewallu): všechny porty v jednom bridge
(`vlan-filtering=yes`, vše ve VLAN 1 bez tagu), management přes DHCP na bridgi,
NTP, vypnuté zbytečné služby. AP: rádia vypnutá, SSID přijdou až z CAPsMANu po
adopci. Firewall: WAN ether1 (DHCP klient), LAN bridge 192.168.88.1/24 s DHCP
serverem, zónový firewall (fwmatrix). Značka role v komentáři bridge
`mtc-default:<role>` – podle ní controller po adopci pozná, že konfigurace je
naše a zařízení může řídit bez resetu.

Ohlášení: scheduler `mtc-enroll` každou minutu zkusí `/enroll/<klíč lokality>`;
zařízení se zaregistruje jako čekající (schvaluje se ručně). Po instalaci
check-inu (adopce, krok 2) se scheduler sám odstraní. Skript nezávisí na
sériovém čísle – jeden soubor pro všechny kusy dané role a lokality.
"""

from __future__ import annotations

from . import fwmatrix
from .routeros import SCRIPT_POLICY, FetchOpts, q, q_source

ROLES = {
    "switch": "switch",
    "ap": "AP",
    "switch_ap": "switch + AP",
    "switch_capsman": "switch + CAPsMAN",
    "firewall": "firewall / router",
}
# role zařízení v controlleru (device.role) a role řízeného režimu
DEVICE_ROLE = {"switch": "switch", "ap": "ap", "switch_ap": "ap", "switch_capsman": "switch", "firewall": "gateway"}
MARK = "mtc-default:"
LAN_ADDR, LAN_NET, LAN_POOL = "192.168.88.1/24", "192.168.88.0/24", "192.168.88.10-192.168.88.254"


def enroll_source(enroll_url: str, opts: FetchOpts) -> str:
    """Zdroj skriptu mtc-enroll: dokud zařízení nemá check-in, zkouší se ohlásit."""
    return f""":if ([:len [/system script find name="mtc-checkin"]] > 0) do={{
  /system scheduler remove [find name="mtc-enroll"]
  /system script remove [find name="mtc-enroll"]
}} else={{
  /file remove [find name="mtc-enroll.rsc"]
  :do {{
    /tool fetch url={q(enroll_url)} dst-path="mtc-enroll.rsc" check-certificate={opts.check_certificate}
    :delay 1s
    /import file-name="mtc-enroll.rsc"
  }} on-error={{ :log info "mtc: controller zatim nedostupny nebo zarizeni ceka na schvaleni" }}
  /file remove [find name="mtc-enroll.rsc"]
}}
"""


def _wait_ports() -> str:
    return (":local i 0\n"
            ":while ($i < 90 && [:len [/interface ethernet find]] = 0) do={ :delay 1s; :set i ($i + 1) }\n"
            ":delay 5s\n")


def _system(ntp: list[str], tz: str, firewall: bool) -> list[str]:
    return [
        ':local serial ""',
        ":do { :set serial [/system routerboard get serial-number] } on-error={}",
        ':if ([:len $serial] > 0) do={ /system identity set name=("mtc-" . $serial) }',
        f"/system clock set time-zone-autodetect=no time-zone-name={q(tz)}",
        f"/system ntp client set enabled=yes servers={q(','.join(ntp))}",
        ':foreach s in={"telnet";"ftp";"www";"api"} do={ :do { /ip service set [find name=$s] disabled=yes } on-error={} }',
        "/tool mac-server set allowed-interface-list=none",
        f"/tool mac-server mac-winbox set allowed-interface-list={'LAN' if firewall else 'all'}",
        f"/ip neighbor discovery-settings set discover-interface-list={'LAN' if firewall else 'all'}",
    ]


def script(role: str, enroll_url: str, opts: FetchOpts, *, ntp: list[str] | None = None,
           tz: str = "Europe/Prague") -> str:
    if role not in ROLES:
        raise ValueError(f"neznámá role {role!r}")
    ntp = ntp or ["tik.cesnet.cz", "tak.cesnet.cz"]
    fw = role == "firewall"
    L: list[str] = []
    L.append(f'/interface bridge add name=bridge protocol-mode=rstp vlan-filtering=no pvid=1 comment="{MARK}{role}"')
    if fw:
        L.append(':foreach p in=[/interface ethernet find where default-name!="ether1"] do={ '
                 '/interface bridge port add bridge=bridge interface=$p comment="mtc" }')
    else:
        L.append(':foreach p in=[/interface ethernet find] do={ /interface bridge port add bridge=bridge interface=$p comment="mtc" }')
    # VLAN filtrování od začátku – řízení pak jen přidává VLAN, nic nepřepíná
    L.append("/interface bridge set bridge vlan-filtering=yes")
    if role in ("ap", "switch_ap"):
        # rádia mlčí, dokud je po adopci nepřevezme CAPsMAN lokality
        L.append(":do { /interface wifi disable [find] } on-error={}")
        L.append(":do { /interface wireless disable [find] } on-error={}")
    if fw:
        L += [
            '/ip dhcp-client add interface=ether1 use-peer-dns=yes add-default-route=yes comment="mtc WAN" disabled=no',
            f'/ip address add address={LAN_ADDR} interface=bridge comment="mtc LAN"',
            f'/ip pool add name=mtc-lan ranges={LAN_POOL} comment="mtc"',
            '/ip dhcp-server add name=mtc-lan interface=bridge address-pool=mtc-lan lease-time=8h comment="mtc" disabled=no',
            f'/ip dhcp-server network add address={LAN_NET} gateway=192.168.88.1 dns-server=192.168.88.1 comment="mtc"',
            "/ip dns set allow-remote-requests=yes",
            fwmatrix.render({"WAN": ["ether1"], "LAN": ["bridge"]},
                            [{"src": "LAN", "dst": "WAN", "action": "accept"}], [LAN_NET]).rstrip(),
        ]
    else:
        L += ['/ip dhcp-client add interface=bridge use-peer-dns=yes add-default-route=yes comment="mtc" disabled=no',
              "/ip dns set allow-remote-requests=no"]
    L += _system(ntp, tz, fw)
    enroll = enroll_source(enroll_url, opts)
    L += [
        "# --- ohlaseni controlleru (do adopce) ---",
        '/system scheduler remove [find name="mtc-enroll"]',
        '/system script remove [find name="mtc-enroll"]',
        f'/system script add name="mtc-enroll" policy={SCRIPT_POLICY} comment="mtc" source={q_source(enroll)}',
        f'/system scheduler add name="mtc-enroll" interval=1m start-date=[/system clock get date] start-time=00:00:00 '
        f'policy={SCRIPT_POLICY} comment="mtc" on-event="/system script run mtc-enroll"',
    ]
    body = "\n".join(L)
    return (f"# MikroTik controller - vychozi konfigurace, role {role}\n"
            f"# Netinstall: netinstall -s mtc-default.rsc ... (vlastni vychozi konfigurace)\n"
            "{\n" + _wait_ports() + body + '\n:log warning "mtc: vychozi konfigurace nastavena, ohlasuji se controlleru"\n}\n')


def one_liner(default_url: str, opts: FetchOpts) -> str:
    """Pro běžící zařízení: stáhnout výchozí konfiguraci a resetovat na ni (smaže stávající konfiguraci)."""
    # starší zařízení mají trvalé soubory jen ve flash/ (jinde jsou v RAM a reset by je smazal)
    return ('{ :local p ""; :if ([:len [/file find name="flash" type="directory"]] > 0) do={ :set p "flash/" }; '
            f'/tool fetch url={q(default_url)} dst-path=($p . "mtc-default.rsc") check-certificate={opts.check_certificate}; '
            ':delay 2s; /system reset-configuration no-defaults=yes keep-users=yes skip-backup=no '
            'run-after-reset=($p . "mtc-default.rsc") }')


def role_from_inventory(inv: dict) -> str | None:
    """Role z naší výchozí konfigurace (komentář bridge), jinak None."""
    v = inv.get("bridges")
    for b in v if isinstance(v, list) else []:
        c = str((b or {}).get("comment") or "")
        if c.startswith(MARK) and c[len(MARK):] in ROLES:
            return c[len(MARK):]
    return None
