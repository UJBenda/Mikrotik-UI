"""Release notes RouterOS: changelogy verzí a výběr změn, které se týkají zařízení.

Zdroj: https://download.mikrotik.com/routeros/<verze>/CHANGELOG (prostý text):

    What's new in 7.18.1 (2025-Feb-28 13:31):

    *) bridge - fixed ...;
    !) ... důležitá změna (chování, kompatibilita)

Changelog verze obsahuje jen změny té verze, pro přechod 7.19.4 → 7.20.2 je
potřeba 7.20, 7.20.1 a 7.20.2. Worker jednou denně stáhne chybějící changelogy
pro verze, na které mají zařízení k dispozici aktualizaci.

Controller tím potřebuje odchozí HTTPS na download.mikrotik.com; bez něj se
release notes prostě nezobrazí (nic jiného na tom nezávisí).
"""

from __future__ import annotations

import logging
import re
import ssl
import urllib.error
import urllib.request
from typing import Any, Callable

from . import ros_versions
from .db import Database, operator_scope

log = logging.getLogger("mtc.releases")

URL = "https://download.mikrotik.com/routeros/{v}/CHANGELOG"
MAX_SIZE = 512 * 1024
VERSION_RE = re.compile(r"^\d+\.\d+(\.\d+)?((beta|rc)\d+)?$")
_HEAD = re.compile(r"What's new in (\S+) \(([^)]*)\)")
_ENTRY = re.compile(r"^([*!])\)\s*(?:([\w./+-]+(?: [\w./+-]+)?) - )?(.*)$")
MAX_PATCH = 12  # kolik oprav (7.x.N) zkoušet u přeskočené vedlejší verze

# komponenty, které ovlivňují samotnou správu (tunel, REST, skripty, aktualizace)
ALWAYS = {"system", "console", "routerboard", "package", "upgrade", "api", "rest-api", "www", "fetch",
          "scheduler", "script", "certificate", "sstp", "ppp", "ethernet", "interface", "user", "firewall",
          "defconf", "device-mode", "netinstall", "disk"}

Getter = Callable[[str], bytes | None]


def _http_get(url: str) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": "mtcontroller"})
    try:
        with urllib.request.urlopen(req, timeout=20, context=ssl.create_default_context()) as r:  # noqa: S310
            return r.read(MAX_SIZE + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def parse(text: str) -> dict[str, Any]:
    head = _HEAD.search(text)
    entries = []
    for ln in text.splitlines():
        m = _ENTRY.match(ln.strip())
        if not m:
            continue
        comp = (m.group(2) or "").strip().lower()
        entries.append({"important": m.group(1) == "!", "component": comp,
                        "text": (m.group(3) or "").strip().rstrip(";")})
    return {"version": head.group(1) if head else None, "released": head.group(2) if head else None,
            "entries": entries}


def between(current: str, target: str) -> list[str]:
    """Kandidáti verzí, jejichž changelog se týká přechodu current → target
    (bez current, včetně target). Nové vedlejší verze se zkouší od .0 do target,
    resp. do MAX_PATCH u přeskočených vedlejších verzí (neexistující vrátí 404)."""
    if not VERSION_RE.match(target):
        return []
    c, t = ros_versions.parse(current), ros_versions.parse(target)
    if len(c) < 3 or len(t) < 3 or t <= c or t[0] != c[0]:
        return [target]
    out: list[str] = []
    for minor in range(c[1], t[1] + 1):
        last = t[2] if minor == t[1] else MAX_PATCH
        first = c[2] + 1 if minor == c[1] else 0
        for patch in range(first, last + 1):
            out.append(f"{c[0]}.{minor}" if patch == 0 else f"{c[0]}.{minor}.{patch}")
    if target not in out:
        out.append(target)
    return out


def device_components(inv: dict) -> set[str]:
    """Komponenty changelogu, které na zařízení něco dělají."""
    def has(k: str) -> bool:
        v = inv.get(k)
        return bool(v) if not isinstance(v, list) else any(isinstance(x, dict) for x in v)

    comps = set(ALWAYS)
    if has("bridges") or has("bridge_ports"):
        comps |= {"bridge", "switch", "vlan", "stp"}
    if has("switch"):
        comps |= {"switch", "crs3xx", "crs1xx/2xx"}
    if has("wifi") or has("wifi_clients"):
        comps |= {"wifi", "wifi-qcom", "wifi-qcom-ac", "wifiwave2"}
    if has("wireless") or has("wireless_clients"):
        comps |= {"wireless", "w60g", "nv2"}
    if has("capsman_caps") or has("capsman_caps_legacy"):
        comps |= {"capsman", "caps-man", "wifi"}
    if has("dhcp_servers"):
        comps |= {"dhcpv4-server", "dhcp-server", "dhcpv4", "dhcp"}
    if has("ntp_client") or has("ntp_server"):
        comps |= {"ntp", "sntp"}
    if has("poe_monitor"):
        comps |= {"poe-out", "poe"}
    if has("vlans"):
        comps.add("vlan")
    if any(str(m.get("sfp-module-present")) == "true" for m in inv.get("ethernet_monitor") or [] if isinstance(m, dict)):
        comps.add("sfp")
    return comps


def notes_for(db: Database, current: str, target: str, inv: dict) -> dict[str, Any]:
    """Změny pro přechod current → target, rozdělené na relevantní a ostatní."""
    versions = between(current, target)
    with operator_scope(db) as tx:
        rows = {r["version"]: r for r in tx.fetchall(
            """SELECT version, released_at, changelog FROM ros_release
               WHERE version IN (SELECT jsonb_array_elements_text(%(v)s))""",
            {"v": versions})}
    comps = device_components(inv)
    relevant, other, found = [], 0, []
    for v in versions:
        r = rows.get(v)
        if not r:
            continue
        found.append({"version": v, "released": r["released_at"]})
        for e in parse(r["changelog"])["entries"]:
            if e["important"] or e["component"] in comps:
                relevant.append({**e, "version": v})
            else:
                other += 1
    relevant.sort(key=lambda e: (not e["important"],))
    return {"current": current, "target": target, "versions": found, "relevant": relevant,
            "other_count": other, "complete": target in rows}


def sync(db: Database, get: Getter = _http_get, limit: int = 40) -> int:
    """Stáhne chybějící changelogy pro dostupné aktualizace ve flotile."""
    with operator_scope(db) as tx:
        pairs = tx.fetchall(
            """SELECT DISTINCT split_part(d.ros_version, ' ', 1) AS cur, i.data->>'latest-version' AS lat
               FROM device d JOIN device_inventory i ON i.device_id = d.id AND i.section = 'update'
               WHERE d.state IN ('adopted', 'offline') AND i.data->>'latest-version' IS NOT NULL""")
        have = {r["version"] for r in tx.fetchall("SELECT version FROM ros_release")}
    want: list[str] = []
    for p in pairs:
        for v in between(p["cur"] or "", p["lat"] or ""):
            if v not in have and v not in want:
                want.append(v)
    n = 0
    dead_minor: set[str] = set()  # opravy jdou po sobě: po 404 vyšší opravy téže verze neexistují
    tried = 0
    for v in want:
        minor = ".".join(v.split(".")[:2])
        if v in _missing or (v.count(".") == 2 and minor in dead_minor):
            continue
        if tried >= limit:
            break
        tried += 1
        try:
            raw = get(URL.format(v=v))
        except Exception as e:  # noqa: BLE001 – bez internetu nic nerozbijeme
            log.info("changelog %s nelze stáhnout: %s", v, e)
            break
        if raw is None:
            _missing.add(v)
            if v.count(".") == 2:
                dead_minor.add(minor)
            continue
        if len(raw) > MAX_SIZE:
            continue
        text = raw.decode("utf-8", errors="replace")
        info = parse(text)
        if not info["version"] and not info["entries"]:
            continue  # není to changelog (např. chybová stránka)
        with operator_scope(db) as tx:
            tx.execute("""INSERT INTO ros_release (version, released_at, changelog)
                          VALUES (%(v)s, %(r)s, %(c)s) ON CONFLICT (version) DO NOTHING""",
                       {"v": v, "r": (info["released"] or "")[:40] or None, "c": text})
        n += 1
    return n


# verze, které na serveru neexistují (404) – v rámci běhu workeru se neopakují
_missing: set[str] = set()
