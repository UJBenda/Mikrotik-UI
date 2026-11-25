"""Kontrola zabezpečení lokality (jen čtení).

Skládá nálezy z toho, co controller už ví: audit nastavení zařízení (`hardening.py`, sekce inventury
`hardening`), verze RouterOS (`ros_versions.py`, sekce `update`), zálohy zařízení, synchronizace času
a účty controlleru bez dvoufázového ověření. Z nálezů počítá skóre zařízení a lokality.

Skóre je orientační: 100 bodů minus srážky za nálezy (kritický 30, varování 10, informace 2).
Zařízení s kritickým nálezem nedostane víc než 40, lokalita s kritickým nálezem víc než 49.
"""

from __future__ import annotations

from typing import Any

from . import hardening, health, ros_versions
from .db import Tx

PENALTY = {"critical": 30, "warning": 10, "info": 2}
ORDER = {"critical": 0, "warning": 1, "info": 2}
BACKUP_STALE_DAYS = 8
# o kolik vedlejších verzí (7.x) smí být zařízení pozadu, než je to varování místo informace
STALE_MINORS = 4

# kód nálezu z hardening.py → oblast
_EXPOSURE = {"no_input_firewall", "open_resolver", "socks_enabled", "proxy_enabled", "upnp_enabled", "smb_enabled",
             "snmp_public", "bw_server"}
CATEGORIES = ["exposure", "access", "updates", "backup", "time", "controller"]


def category(code: str) -> str:
    return "exposure" if code in _EXPOSURE else "access"


def score(findings: list[dict]) -> int:
    s = 100 - sum(PENALTY.get(f.get("severity"), 0) for f in findings)
    if any(f.get("severity") == "critical" for f in findings):
        s = min(s, 40)
    return max(0, s)


def level(s: int | None) -> str:
    return "none" if s is None else "ok" if s >= 85 else "warning" if s >= 60 else "critical"


def _f(sev: str, code: str, text: str, cat: str, fix: str = "") -> dict:
    return {"severity": sev, "code": code, "text": text, "fix": fix, "category": cat, "fixable": False, "fix_label": ""}


def version_findings(version: str | None, update: Any) -> list[dict]:
    """Nálezy k verzi RouterOS: nepodporovaná, se známou chybou, zastaralá proti kanálu."""
    out: list[dict] = []
    v = str(version or "")
    if not v:
        return out
    if not ros_versions.is_supported(v):
        out.append(_f("warning", "ros_unsupported",
                      f"RouterOS {v} je starší než {ros_versions.MIN_SUPPORTED} – bez oprav chyb, controller ho neumí řídit",
                      "updates"))
    for i in ros_versions.issues_for(v):
        sev = i.severity if i.severity in PENALTY else "warning"
        out.append(_f(sev, "ros_known_issue", f"RouterOS {v}: {i.text}", "updates"))
    upd = update if isinstance(update, dict) else {}
    latest = str(upd.get("latest-version") or "")
    cur, new = ros_versions.parse(v), ros_versions.parse(latest)
    if latest and new > cur:
        behind = (new[1] - cur[1]) if len(new) > 1 and len(cur) > 1 and new[0] == cur[0] else STALE_MINORS
        sev = "warning" if behind >= STALE_MINORS else "info"
        out.append(_f(sev, "ros_outdated", f"RouterOS {v}, v kanálu {upd.get('channel') or '?'} je {latest}", "updates"))
    return out


def device_findings(dev: dict, inv: dict) -> list[dict]:
    out: list[dict] = []
    hard = inv.get("hardening")
    for f in hardening.fixable([x for x in hard if isinstance(x, dict)] if isinstance(hard, list) else []):
        out.append(dict(f, category=category(str(f.get("code")))))
    out += version_findings(dev.get("ros_version"), inv.get("update"))
    if not dev.get("backup_enabled"):
        out.append(_f("warning", "backup_off", "zálohy konfigurace jsou pro zařízení vypnuté", "backup"))
    elif dev.get("backup_error"):
        out.append(_f("warning", "backup_failed", f"poslední záloha selhala: {dev['backup_error']}", "backup"))
    elif dev.get("backup_days") is None:
        out.append(_f("warning", "backup_none", "zařízení ještě nemá žádnou zálohu konfigurace", "backup"))
    elif dev["backup_days"] > BACKUP_STALE_DAYS:
        out.append(_f("warning", "backup_stale", f"poslední záloha je {int(dev['backup_days'])} dní stará", "backup"))
    ntp = inv.get("ntp_client") if isinstance(inv.get("ntp_client"), dict) else None
    if ntp is not None and str(ntp.get("enabled")).lower() not in ("true", "yes"):
        out.append(_f("info", "ntp_off", "čas se nesynchronizuje (NTP klient vypnutý) – záznamy v logu nemusí sedět",
                      "time", "/system ntp client set enabled=yes servers=<NTP server>"))
    elif health.ntp_unsynced(inv):
        out.append(_f("info", "ntp_unsynced", "NTP klient je zapnutý, ale hodiny nejsou synchronizované", "time"))
    out.sort(key=lambda f: ORDER.get(f["severity"], 3))
    return out


_SECTIONS = ("hardening", "update", "ntp_client", "resource")


def site_check(tx: Tx, tenant_id: Any, site_id: Any) -> dict:
    """Nálezy a skóre zařízení lokality + nálezy controlleru (účty zákazníka bez dvoufázového ověření)."""
    devs = tx.fetchall(
        """SELECT d.id, coalesce(nullif(d.identity, ''), d.serial) AS name, d.serial, d.model, d.role, d.state,
                  d.ros_version, d.backup_enabled, d.backup_error,
                  (SELECT extract(epoch FROM now() - max(b.taken_at)) / 86400 FROM device_backup b
                    WHERE b.device_id = d.id) AS backup_days
           FROM device d WHERE d.site_id = %(s)s AND d.state IN ('adopted', 'offline')
           ORDER BY (d.role = 'gateway') DESC, name""", {"s": str(site_id)})
    inv: dict[str, dict] = {}
    for r in tx.fetchall(
            """SELECT i.device_id, i.section, i.data FROM device_inventory i JOIN device d ON d.id = i.device_id
               WHERE d.site_id = %(s)s AND i.section = ANY(string_to_array(%(sec)s, ','))""",
            {"s": str(site_id), "sec": ",".join(_SECTIONS)}):
        inv.setdefault(str(r["device_id"]), {})[r["section"]] = r["data"]
    plans_open = {str(r["device_id"]) for r in tx.fetchall(
        """SELECT p.device_id FROM config_plan p JOIN device d ON d.id = p.device_id
           WHERE d.site_id = %(s)s AND p.module = 'hardening' AND p.status IN ('draft', 'queued', 'confirming')""",
        {"s": str(site_id)})}
    out_devs = []
    counts = {k: 0 for k in PENALTY}
    cats = {c: {"critical": 0, "warning": 0, "info": 0} for c in CATEGORIES}
    for d in devs:
        di = inv.get(str(d["id"]), {})
        if d.get("backup_days") is not None:
            d["backup_days"] = float(d["backup_days"])
        f = device_findings(d, di)
        for x in f:
            counts[x["severity"]] += 1
            cats[x["category"]][x["severity"]] += 1
        s = score(f)
        out_devs.append({"id": str(d["id"]), "name": d["name"], "serial": d["serial"], "model": d["model"],
                         "role": d["role"], "state": d["state"], "ros_version": d["ros_version"],
                         # bez inventury nevíme nic o nastavení – skóre by bylo nezasloužené
                         "checked": "hardening" in di, "score": s if "hardening" in di else None,
                         "level": level(s if "hardening" in di else None),
                         "findings": f, "plan_open": str(d["id"]) in plans_open})
    users = tx.fetchall(
        """SELECT u.email FROM role_binding rb JOIN app_user u ON u.id = rb.user_id
           WHERE rb.tenant_id = %(t)s AND rb.site_id IS NULL AND u.is_active AND NOT u.totp_confirmed
           ORDER BY u.email""", {"t": str(tenant_id)})
    site_f: list[dict] = []
    if users:
        n = len(users)
        site_f.append(_f("warning", "mfa_missing",
                         f"{n} {'účet' if n == 1 else 'účty' if n < 5 else 'účtů'} zákazníka se do controlleru "
                         f"přihlašuje bez dvoufázového ověření", "controller"))
        site_f[-1]["users"] = [u["email"] for u in users]
    for x in site_f:
        counts[x["severity"]] += 1
        cats[x["category"]][x["severity"]] += 1
    scored = [d["score"] for d in out_devs if d["score"] is not None]
    total: int | None = None
    if scored:
        total = round(sum(scored) / len(scored)) - sum(PENALTY[x["severity"]] for x in site_f)
        if counts["critical"]:
            total = min(total, 49)
        total = max(0, total)
    return {"score": total, "level": level(total), "counts": counts, "categories": cats,
            "devices": out_devs, "site": site_f,
            "unchecked": sum(1 for d in out_devs if not d["checked"])}
