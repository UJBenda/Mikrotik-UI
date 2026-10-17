"""Routy na zařízení přes koncentrátor, ke kterému jsou právě připojená.

Se dvěma koncentrátory nestačí jedna statická routa poolu: po přepnutí
zařízení na vpn2 by správa šla dál přes vpn1. Worker proto (je-li zapnuto
`tunnel.manage_routes`) každou minutu porovná aktivní SSTP relace na
koncentrátorech s routami /32 v hlavní tabulce a srovná je.

- Spravuje jen své routy – označené protokolem PROTO (`ip route … proto 186`);
  ostatních rout se nedotkne. Statická routa celého poolu zůstává jako záloha.
- Koncentrátor, který neodpověděl, se nepovažuje za prázdný: jeho routy zůstanou.
- Adresy z koncentrátoru se validují (musí být v poolu) dřív, než jdou do `ip`.
- Worker potřebuje CAP_NET_ADMIN (drop-in pro systemd, viz docs/install.md).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import subprocess
from typing import Callable
from urllib.parse import urlsplit

from .config import ConcentratorConfig, Settings

log = logging.getLogger("mtc.routes")

PROTO = "186"

Runner = Callable[[list[str], str | None], str]


def _run(args: list[str], stdin: str | None = None) -> str:
    r = subprocess.run(["ip", *args], input=stdin, capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise RuntimeError(f"ip {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def gateway(c: ConcentratorConfig) -> str:
    """Adresa koncentrátoru v síti controlleru (route_via, jinak host z api_url)."""
    gw = c.route_via or urlsplit(c.api_url).hostname or ""
    return str(ipaddress.ip_address(gw))


def current(run: Runner = _run) -> dict[str, str]:
    out = {}
    for r in json.loads(run(["-j", "-4", "route", "show", "proto", PROTO], None) or "[]"):
        dst, gw = str(r.get("dst", "")), r.get("gateway")
        if gw and dst:
            out[dst.split("/")[0]] = gw
    return out


def desired(settings: Settings, sessions: dict[str, dict[str, str] | None]) -> tuple[dict[str, str], set[str]]:
    """Vrací ({ip zařízení: brána}, {brány koncentrátorů, které neodpověděly}).
    Pořadí koncentrátorů v konfiguraci rozhoduje, je-li zařízení vidět na dvou."""
    pool = ipaddress.ip_network(settings.tunnel.pool)
    want: dict[str, str] = {}
    unknown: set[str] = set()
    for c in settings.tunnel.concentrators:
        gw = gateway(c)
        s = sessions.get(c.name)
        if s is None:
            unknown.add(gw)
            continue
        for addr in s.values():
            try:
                ip = ipaddress.ip_address(str(addr).split("/")[0])
            except ValueError:
                continue
            if ip in pool and str(ip) not in want:
                want[str(ip)] = gw
    return want, unknown


def plan(have: dict[str, str], want: dict[str, str], unknown: set[str]) -> list[str]:
    """Příkazy pro `ip -batch`."""
    cmds = [f"route replace {ip}/32 via {gw} proto {PROTO}" for ip, gw in sorted(want.items()) if have.get(ip) != gw]
    cmds += [f"route del {ip}/32 proto {PROTO}" for ip, gw in sorted(have.items())
             if ip not in want and gw not in unknown]
    return cmds


def sync(settings: Settings, providers, run: Runner = _run) -> int:
    """Srovná routy podle aktivních relací. Vrací počet změn."""
    sessions: dict[str, dict[str, str] | None] = {}
    for p in providers:
        try:
            sessions[p.name] = p.active_sessions()
        except Exception as e:  # noqa: BLE001 – nedostupný koncentrátor
            log.warning("routy: koncentrátor %s neodpověděl: %s", p.name, e)
            sessions[p.name] = None
        finally:
            close = getattr(getattr(p, "api", None), "close", None)
            if close:
                close()
    want, unknown = desired(settings, sessions)
    cmds = plan(current(run), want, unknown)
    if cmds:
        run(["-force", "-batch", "-"], "\n".join(cmds) + "\n")
        log.info("routy: %d změn (%s)", len(cmds), "; ".join(cmds[:5]))
    return len(cmds)
