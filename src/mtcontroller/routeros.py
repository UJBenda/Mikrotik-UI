"""Generování RouterOS v7 skriptů (.rsc), které zařízení stahuje a importuje.

Zásady:
- Všechny hodnoty vkládané do skriptu jdou přes `q()` (escapování pro "...").
  Vstupy zvenku (sériové číslo, model) se do skriptů nevkládají vůbec.
- Každý skript je obalený v `{ ... }`, aby :local proměnné platily pro celý soubor.
- Vše, co controller na zařízení vytvoří, má jméno nebo komentář začínající
  „mtc“, takže jde jednoznačně najít a odstranit (deprovision).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Politiky skriptu check-inu a jeho scheduleru: musí stačit na provisioning
# (uživatelé, služby, certifikát, firewall, fetch), ale bez interaktivních přístupů.
SCRIPT_POLICY = "ftp,reboot,read,write,policy,test,password,sensitive"
# Skupina servisního účtu controlleru: jen API/REST, žádné SSH/Winbox/web login.
MTC_GROUP_POLICY = "read,write,policy,test,reboot,sensitive,api,rest-api,ftp"
MTC_GROUP_POLICY_FALLBACK = "read,write,policy,test,reboot,sensitive,api,web,ftp"

SSTP_IFACE = "mtc-sstp"
REST_CERT = "mtc-rest"
API_USER = "mtc-ctl"

_SAFE = re.compile(r"^[\x20-\x7e]*$")


def q(value: str) -> str:
    """Řetězcový literál RouterOS ("...") s escapováním."""
    if not _SAFE.match(value):
        raise ValueError("do RouterOS skriptu lze vložit jen tisknutelné ASCII")
    out = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("?", "\\?")
    return f'"{out}"'


def q_source(source: str) -> str:
    """Literál pro `source=` skriptu – víceřádkový text."""
    lines = source.strip("\n").split("\n")
    for ln in lines:
        if not _SAFE.match(ln):
            raise ValueError("zdroj skriptu musí být tisknutelné ASCII")
    body = "\\r\\n".join(
        ln.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("?", "\\?")
        for ln in lines
    )
    return f'"{body}"'


@dataclass(frozen=True)
class FetchOpts:
    check_certificate: str = "yes-without-crl"

    @property
    def verify_sstp(self) -> str:
        return "no" if self.check_certificate == "no" else "yes"


# --- adopce ------------------------------------------------------------------


def adopt_one_liner(base_url: str, token: str, opts: FetchOpts) -> str:
    """Řádek, který technik vloží do terminálu MikroTiku."""
    url = f"{base_url}/adopt/{token}"
    return (f"/tool fetch url={q(url)} dst-path=\"mtc-adopt.rsc\" "
            f"check-certificate={opts.check_certificate}; :delay 2s; "
            f"/import file-name=\"mtc-adopt.rsc\"")


def adopt_stage1(register_url: str, opts: FetchOpts) -> str:
    """Krok 1: zjistí identitu zařízení a zaregistruje ho. Odpověď (krok 2)
    rovnou importuje."""
    return f"""# MikroTik controller - adopce, krok 1
{{
:local serial ""
:do {{ :set serial [/system routerboard get serial-number] }} on-error={{}}
:if ($serial = "") do={{ :do {{ :set serial [/system license get system-id] }} on-error={{}} }}
:if ($serial = "") do={{ :error "mtc: nelze zjistit seriove cislo" }}
:local model ""
:do {{ :set model [/system routerboard get model] }} on-error={{}}
:if ($model = "") do={{ :set model [/system resource get board-name] }}
:local ver [/system resource get version]
:local mac ""
:do {{ :set mac [/interface ethernet get [find default-name="ether1"] mac-address] }} on-error={{}}
:local body ("{{\\"serial\\":\\"" . $serial . "\\",\\"model\\":\\"" . $model . "\\",\\"version\\":\\"" . $ver . "\\",\\"mac\\":\\"" . $mac . "\\"}}")
/file remove [find name="mtc-stage2.rsc"]
/tool fetch url={q(register_url)} http-method=post http-header-field="Content-Type: application/json" http-data=$body dst-path="mtc-stage2.rsc" check-certificate={opts.check_certificate}
:delay 2s
/import file-name="mtc-stage2.rsc"
/file remove [find name="mtc-stage2.rsc"]
/file remove [find name="mtc-adopt.rsc"]
}}
"""


def checkin_source(checkin_url: str, key: str, opts: FetchOpts) -> str:
    """Zdroj skriptu mtc-checkin (spouští ho scheduler každou minutu)."""
    return f""":local url {q(checkin_url)}
:local hdr {q("Content-Type: application/json,Authorization: Bearer " + key)}
:local sstp "none"
:do {{ :if ([/interface sstp-client get [find name="{SSTP_IFACE}"] running]) do={{ :set sstp "up" }} else={{ :set sstp "down" }} }} on-error={{}}
:local fp ""
:do {{ :set fp [/certificate get [find name="{REST_CERT}"] fingerprint] }} on-error={{}}
:local ver [/system resource get version]
:local up [/system resource get uptime]
:local cpu [/system resource get cpu-load]
:local body ("{{\\"version\\":\\"" . $ver . "\\",\\"uptime\\":\\"" . $up . "\\",\\"cpu\\":" . $cpu . ",\\"sstp\\":\\"" . $sstp . "\\",\\"fp\\":\\"" . $fp . "\\"}}")
/file remove [find name="mtc-cmd.rsc"]
:do {{
  /tool fetch url=$url http-method=post http-header-field=$hdr http-data=$body dst-path="mtc-cmd.rsc" check-certificate={opts.check_certificate}
  :delay 1s
  /import file-name="mtc-cmd.rsc"
}} on-error={{ :log warning "mtc: check-in selhal" }}
/file remove [find name="mtc-cmd.rsc"]
"""


def adopt_stage2(checkin_url: str, key: str, opts: FetchOpts) -> str:
    """Krok 2 (odpověď na registraci): nainstaluje check-in skript a scheduler."""
    src = checkin_source(checkin_url, key, opts)
    return f"""# MikroTik controller - adopce, krok 2
{{
/system scheduler remove [find name="mtc-checkin"]
/system script remove [find name="mtc-checkin"]
/system script add name="mtc-checkin" policy={SCRIPT_POLICY} dont-require-permissions=no comment="mtc" source={q_source(src)}
/system scheduler add name="mtc-checkin" interval=1m policy={SCRIPT_POLICY} comment="mtc" on-event="/system script run mtc-checkin"
:log info "mtc: zarizeni zaregistrovano, ceka na schvaleni v controlleru"
:execute script="/system script run mtc-checkin"
}}
"""


def error_script(message: str) -> str:
    return f'{{ :log error {q("mtc: " + message)}; :error {q("mtc: " + message)} }}\n'


NOTHING = "# mtc: nic k provedeni\n"


# --- příkazy -------------------------------------------------------------------


def wrap_command(ack_url: str, key: str, body: str, opts: FetchOpts, tail: str = "") -> str:
    """Obalí tělo příkazu: při chybě nahlásí `failed`, jinak `done`.
    `tail` se spustí až po odeslání výsledku (reboot, úklid po deprovisionu)."""
    return f"""{{
:local ok true
:do {{
{body.rstrip()}
}} on-error={{ :set ok false }}
:local st "done"
:if (!$ok) do={{ :set st "failed" }}
:do {{ /tool fetch url=({q(ack_url + "&status=")} . $st) http-method=post http-header-field={q("Authorization: Bearer " + key)} output=none check-certificate={opts.check_certificate} }} on-error={{ :log warning "mtc: ack selhal" }}
{tail.rstrip()}
}}
"""


@dataclass(frozen=True)
class ProvisionParams:
    # adresa, ze které na zařízení přichází správa: koncentrátor přeloží
    # (src-nat) controller na svou tunelovou adresu, viz concentrator.py
    mgmt_source: str
    connect_host: str
    sstp_user: str
    sstp_password: str
    api_password: str


def provision_body(p: ProvisionParams, opts: FetchOpts) -> str:
    """Založí SSTP tunel, servisní účet API, certifikát pro REST a firewall,
    který z tunelu pustí jen správu z koncentrátoru. Idempotentní.

    Na zařízení se nepřidává žádná routa: správa přichází z tunelové adresy
    koncentrátoru (přímo připojená přes PPP), takže check-in i ostatní provoz
    jdou dál normální cestou."""
    ctrl = p.mgmt_source
    return f""":log info "mtc: provisioning zacina"
/interface sstp-client remove [find name="{SSTP_IFACE}"]
/interface sstp-client add name="{SSTP_IFACE}" connect-to={q(p.connect_host)} port=443 user={q(p.sstp_user)} password={q(p.sstp_password)} verify-server-certificate={opts.verify_sstp} add-sni=yes add-default-route=no dial-on-demand=no profile=default-encryption authentication=mschap2 comment="mtc" disabled=no
/ip route remove [find comment="mtc"]
:if ([:len [/certificate find name="{REST_CERT}"]] = 0) do={{
  /certificate add name="{REST_CERT}" common-name={q(p.sstp_user)} key-size=prime256v1 days-valid=3650 key-usage=tls-server
  /certificate sign "{REST_CERT}"
  :delay 5s
}}
/ip service set www-ssl certificate="{REST_CERT}" disabled=no
# ma-li www-ssl omezene adresy, pridej controller (jinak by REST pres tunel nesel)
:do {{
  :local allowed [/ip service get www-ssl address]
  :if ([:len $allowed] > 0 && [:len [:find $allowed {q(ctrl + "/32")}]] = 0) do={{
    /ip service set www-ssl address=($allowed, {q(ctrl + "/32")})
  }}
}} on-error={{ :log warning "mtc: nepodarilo se doplnit adresu controlleru do www-ssl" }}
:if ([:len [/user group find name="mtc"]] = 0) do={{
  :do {{ /user group add name="mtc" policy={MTC_GROUP_POLICY} comment="mtc" }} on-error={{ /user group add name="mtc" policy={MTC_GROUP_POLICY_FALLBACK} comment="mtc" }}
}} else={{
  :do {{ /user group set [find name="mtc"] policy={MTC_GROUP_POLICY} }} on-error={{ /user group set [find name="mtc"] policy={MTC_GROUP_POLICY_FALLBACK} }}
}}
:if ([:len [/user find name="{API_USER}"]] = 0) do={{
  /user add name="{API_USER}" group="mtc" password={q(p.api_password)} address={q(ctrl + "/32")} comment="mtc"
}} else={{
  :if ([/user get [find name="{API_USER}"] comment] != "mtc") do={{ :error "mtc: uzivatel {API_USER} existuje a nepatri controlleru" }}
  /user set [find name="{API_USER}"] group="mtc" password={q(p.api_password)} address={q(ctrl + "/32")}
}}
:local port [/ip service get www-ssl port]
/ip firewall filter remove [find comment~"^mtc:"]
/ip firewall filter add chain=input in-interface="{SSTP_IFACE}" src-address={q(ctrl)} protocol=tcp dst-port=$port action=accept comment="mtc: sprava REST"
/ip firewall filter add chain=input in-interface="{SSTP_IFACE}" src-address={q(ctrl)} protocol=icmp action=accept comment="mtc: sprava ping"
/ip firewall filter add chain=input in-interface="{SSTP_IFACE}" action=drop comment="mtc: drop tunnel"
/ip firewall filter add chain=forward in-interface="{SSTP_IFACE}" action=drop comment="mtc: drop tunnel forward"
:if ([:len [/ip firewall filter find]] > 4) do={{
  /ip firewall filter move [find comment="mtc: drop tunnel forward"] destination=([/ip firewall filter find]->0)
  /ip firewall filter move [find comment="mtc: drop tunnel"] destination=([/ip firewall filter find]->0)
  /ip firewall filter move [find comment="mtc: sprava ping"] destination=([/ip firewall filter find]->0)
  /ip firewall filter move [find comment="mtc: sprava REST"] destination=([/ip firewall filter find]->0)
}}
:log info "mtc: provisioning hotov"
"""


def switch_concentrator_body(connect_host: str) -> str:
    return (f'/interface sstp-client set [find name="{SSTP_IFACE}"] connect-to={q(connect_host)}\n'
            f':log warning {q("mtc: prepnuto na koncentrator " + connect_host)}\n')


REBOOT_TAIL = ':execute ":delay 5s; /system reboot"\n'

DEPROVISION_TAIL = f""":log warning "mtc: odebrani ze spravy controlleru"
/ip firewall filter remove [find comment~"^mtc:"]
/ip route remove [find comment="mtc"]
/interface sstp-client remove [find name="{SSTP_IFACE}"]
/user remove [find name="{API_USER}" comment="mtc"]
/user group remove [find name="mtc" comment="mtc"]
/system scheduler remove [find name="mtc-checkin"]
/system script remove [find name="mtc-checkin"]
"""
