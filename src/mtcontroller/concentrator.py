"""Generátor konfigurace koncentrátoru (RouterOS v7) – `mtc-admin concentrator-script NAME`.

Předpoklady na koncentrátoru (ručně, jednou):
1. certifikát pro SSTP s **RSA** klíčem (SSTP server RouterOS nabízí jen RSA šifry –
   s ECDSA certifikátem spojení skončí „no common ciphers“): v produkci
   `/certificate enable-ssl-certificate dns-name=<connect_host>` (Let's Encrypt,
   potřebuje dočasně port 80 z internetu), v labu RSA 2048 podepsaný lokální CA,
2. soubor api_password_file na controlleru (vytvoří install.sh); heslo se vloží do skriptu.

Skript je idempotentní: lze ho pustit znovu po změně konfigurace.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from . import ipam
from .config import ConcentratorConfig, Settings
from .routeros import q, signed_cert
from .tunnel.mikrotik import PPP_PROFILE


def concentrator_script(settings: Settings, c: ConcentratorConfig, api_password: str) -> str:
    ctrl = settings.tunnel.controller_ip
    local = str(ipam.concentrator_address(settings.tunnel.pool))
    api_port = urlsplit(c.api_url).port or 443
    if api_port == 443:
        raise ValueError("REST API koncentrátoru nesmí být na 443 (ten patří SSTP) – použij např. https://x:8443")
    return f"""# MikroTik controller - koncentrator {c.name} ({c.connect_host})
# Vygenerovano: mtc-admin concentrator-script {c.name}
# Vloz do terminalu koncentratoru nebo nahraj jako soubor a /import.
{{
:local ctrl {q(ctrl)}
:local sstpHost {q(c.connect_host)}

# heslo uctu controlleru (z {c.api_password_file})
:local apiPassword {q(api_password)}

# --- controller_ip nesmi byt adresa tohoto koncentratoru ---
:if ([:len [/ip address find address~("^" . $ctrl . "/")]] > 0) do={{ :error ("mtc: controller_ip " . $ctrl . " je adresa tohoto koncentratoru, v config.toml nastav IP Debianu") }}

# --- certifikat SSTP pro $sstpHost: musi byt RSA (SSTP server nabizi jen RSA sifry,
# s ECDSA klient hlasi "no common ciphers") ---
:local sstpCert ""
:foreach c in=[/certificate find common-name=$sstpHost private-key=yes] do={{
  :local kt "rsa"
  :do {{ :set kt [/certificate get $c key-type] }} on-error={{}}
  :if ($sstpCert = "" && $kt ~ "[Rr][Ss][Aa]") do={{ :set sstpCert [/certificate get $c name] }}
}}
:if ($sstpCert = "") do={{ :error ("mtc: chybi RSA certifikat pro " . $sstpHost . " (SSTP neumi ECDSA) - Let's Encrypt: /certificate enable-ssl-certificate dns-name=" . $sstpHost . " nebo lab: key-size=2048 podepsany lokalni CA") }}

# --- seznam rozhrani tunelu a PPP profil ---
:if ([:len [/interface list find name="TUNNELS"]] = 0) do={{ /interface list add name="TUNNELS" comment="mtc" }}
:if ([:len [/ppp profile find name="{PPP_PROFILE}"]] = 0) do={{ /ppp profile add name="{PPP_PROFILE}" }}
/ppp profile set [find name="{PPP_PROFILE}"] local-address={q(local)} interface-list="TUNNELS" only-one=yes change-tcp-mss=yes use-encryption=required use-compression=no comment="mtc"

# --- REST API pro controller (self-signed, controller ho pinuje otiskem) ---
{signed_cert("mtc-api", c.name)}/ip service set www-ssl certificate="mtc-api" port={api_port} address=($ctrl . "/32") disabled=no
:if ([:len [/user group find name="mtc-controller"]] = 0) do={{
  :do {{ /user group add name="mtc-controller" policy=read,write,api,rest-api comment="mtc" }} on-error={{ /user group add name="mtc-controller" policy=read,write,api,web comment="mtc" }}
}}
:if ([:len [/user find name={q(c.api_user)}]] = 0) do={{
  /user add name={q(c.api_user)} group="mtc-controller" password=$apiPassword address=($ctrl . "/32") comment="mtc"
}} else={{
  /user set [find name={q(c.api_user)}] group="mtc-controller" password=$apiPassword address=($ctrl . "/32")
}}

# --- bez ICMP redirectu: pri vypadku tunelu by controller dostal redirect na vychozi branu
# a kernel by si cachoval spatnou routu na pool ---
/ip settings set send-redirects=no
# RouterOS 7.24+: sluzba reverse-proxy sedi na 443, na koncentratoru patri 443 jen SSTP
:do {{ /ip service set reverse-proxy disabled=yes }} on-error={{}}
# --- SSTP server na 443 (az po presunu www-ssl z 443 na port REST) ---
/interface sstp-server server set enabled=yes port=443 certificate=$sstpCert authentication=mschap2 default-profile="{PPP_PROFILE}"

# --- firewall: tunely se nevidi, do tunelu smi jen controller ---
/ip firewall address-list remove [find list="mtc-controller"]
/ip firewall address-list add list="mtc-controller" address=$ctrl comment="mtc"
/ip firewall filter remove [find comment~"^mtc:"]
/ip firewall filter add chain=input protocol=tcp dst-port=443 action=accept comment="mtc: SSTP"
/ip firewall filter add chain=input src-address-list="mtc-controller" protocol=tcp dst-port={api_port} action=accept comment="mtc: controller API"
/ip firewall filter add chain=input in-interface-list="TUNNELS" action=drop comment="mtc: drop input z tunelu"
/ip firewall filter add chain=forward in-interface-list="TUNNELS" connection-state=established,related action=accept comment="mtc: odpovedi z tunelu"
/ip firewall filter add chain=forward src-address-list="mtc-controller" out-interface-list="TUNNELS" protocol=tcp dst-port=443 action=accept comment="mtc: controller REST do tunelu"
/ip firewall filter add chain=forward src-address-list="mtc-controller" out-interface-list="TUNNELS" protocol=icmp action=accept comment="mtc: controller ping do tunelu"
/ip firewall filter add chain=forward in-interface-list="TUNNELS" action=drop comment="mtc: drop z tunelu"
/ip firewall filter add chain=forward out-interface-list="TUNNELS" action=drop comment="mtc: drop do tunelu"
# sprava z controlleru do tunelu se preklada na tunelovou adresu koncentratoru:
# zarizeni nepotrebuje zadnou routu a skutecnou IP controlleru nevidi
/ip firewall nat remove [find comment~"^mtc:"]
/ip firewall nat add chain=srcnat src-address-list="mtc-controller" out-interface-list="TUNNELS" action=src-nat to-addresses={q(local)} comment="mtc: controller do tunelu"
:if ([:len [/ip firewall nat find dynamic=no]] > 1) do={{
  /ip firewall nat move [find comment="mtc: controller do tunelu"] destination=([/ip firewall nat find dynamic=no]->0)
}}
# dynamicka pravidla (fasttrack dummy) preskocit - pred ne nejde presouvat
:if ([:len [/ip firewall filter find dynamic=no]] > 8) do={{
  :foreach c in={{"mtc: drop do tunelu";"mtc: drop z tunelu";"mtc: controller ping do tunelu";"mtc: controller REST do tunelu";"mtc: odpovedi z tunelu";"mtc: drop input z tunelu";"mtc: controller API";"mtc: SSTP"}} do={{
    /ip firewall filter move [find comment=$c] destination=([/ip firewall filter find dynamic=no]->0)
  }}
}}
:log info "mtc: koncentrator nastaven"
:put ("mtc: hotovo. Otisk certifikatu mtc-api (api_fingerprint do config.toml): " . [/certificate get [find name="mtc-api"] fingerprint])
}}
"""
