"""Modul NTP – první modul, který zapisuje.

Politika lokality (site_policy, module='ntp'):
    {"primary_device_id": "<uuid>", "upstream": ["tik.cesnet.cz", "tak.cesnet.cz"],
     "timezone": "Europe/Prague"}

Požadovaný stav:
- primární zařízení: NTP klient na upstream servery, NTP server zapnutý,
- ostatní zařízení lokality: NTP klient na adresu primárního zařízení ve
  stejné síti (+ první upstream jako záloha),
- všude stejná časová zóna.
Z rozdílu proti inventuře vzniká plán (skript + popis), který admin schválí.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import analysis
from .routeros import q

_HOST = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
_TZ = re.compile(r"^[A-Za-z_]+(/[A-Za-z0-9_+-]+){0,2}$")
DEFAULT_UPSTREAM = ["tik.cesnet.cz", "tak.cesnet.cz"]
DEFAULT_TZ = "Europe/Prague"
SECTIONS = ["ntp_client", "ntp_server", "clock", "addresses"]


class PolicyError(ValueError):
    pass


def validate_policy(data: dict) -> dict:
    upstream = data.get("upstream") or DEFAULT_UPSTREAM
    if not isinstance(upstream, list) or not 1 <= len(upstream) <= 4:
        raise PolicyError("upstream: 1 až 4 servery")
    for h in upstream:
        if not isinstance(h, str) or not _HOST.match(h):
            raise PolicyError(f"neplatný NTP server: {h!r}")
    tz = data.get("timezone") or DEFAULT_TZ
    if not isinstance(tz, str) or not _TZ.match(tz):
        raise PolicyError("neplatná časová zóna")
    primary = data.get("primary_device_id")
    if not primary:
        raise PolicyError("chybí primary_device_id")
    return {"primary_device_id": str(primary), "upstream": list(upstream), "timezone": tz}


@dataclass
class Desired:
    client_servers: list[str]
    server_enabled: bool | None  # None = neměnit (u klientů necháváme NTP server, jak je)
    timezone: str


def desired_for(policy: dict, device_id: str, inv: dict, primary_inv: dict | None) -> tuple[Desired | None, str | None]:
    if device_id == policy["primary_device_id"]:
        return Desired(policy["upstream"], True, policy["timezone"]), None
    if primary_inv is None:
        return None, "inventura primárního zařízení zatím není"
    addr = analysis.reachable_address(primary_inv, inv)
    if not addr:
        return None, "primární NTP není ve stejné síti jako toto zařízení"
    return Desired([addr, policy["upstream"][0]], None, policy["timezone"]), None


def changes(desired: Desired, inv: dict) -> list[dict]:
    cur = analysis.ntp_status(inv)
    out = []
    if not cur["client_enabled"] or cur["servers"] != desired.client_servers:
        out.append({"what": "ntp_client", "from": {"enabled": cur["client_enabled"], "servers": cur["servers"]},
                    "to": {"enabled": True, "servers": desired.client_servers}})
    if desired.server_enabled is not None and cur["server_enabled"] != desired.server_enabled:
        out.append({"what": "ntp_server", "from": cur["server_enabled"], "to": desired.server_enabled})
    if cur["timezone"] != desired.timezone:
        out.append({"what": "timezone", "from": cur["timezone"], "to": desired.timezone})
    return out


def script(desired: Desired, chg: list[dict]) -> str:
    lines = []
    whats = {c["what"] for c in chg}
    if "ntp_client" in whats:
        lines.append(f"/system ntp client set enabled=yes mode=unicast servers={q(','.join(desired.client_servers))}")
    if "ntp_server" in whats:
        lines.append(f"/system ntp server set enabled={'yes' if desired.server_enabled else 'no'}")
    if "timezone" in whats:
        lines.append(f"/system clock set time-zone-autodetect=no time-zone-name={q(desired.timezone)}")
    lines.append(':log info "mtc: NTP nastaveno podle politiky lokality"')
    return "\n".join(lines) + "\n"


def summary(chg: list[dict]) -> str:
    parts = []
    for c in chg:
        if c["what"] == "ntp_client":
            parts.append("NTP klient → " + ", ".join(c["to"]["servers"]))
        elif c["what"] == "ntp_server":
            parts.append("NTP server " + ("zapnout" if c["to"] else "vypnout"))
        elif c["what"] == "timezone":
            parts.append(f"časová zóna {c['from'] or '?'} → {c['to']}")
    return "; ".join(parts)
