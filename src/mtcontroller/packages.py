"""Hlídání balíčků RouterOS: správný Wi-Fi ovladač a shodné verze balíčků.

Wi-Fi ovladače v RouterOS v7:
- `wifi-qcom`    – 802.11ax (cAP ax, hAP ax², ax³, wAP ax …): CAPsMAN, VLAN v datapath, více hesel s VLAN;
- `wifi-qcom-ac` – 802.11ac Wave2 na ARM (cAP ac, hAP ac², ac³, Audience, wAP ac …): CAPsMAN ano,
                   VLAN v datapath ne (jen přes bridge na AP), více hesel s VLAN ne;
- `wireless`     – starý ovladač (hAP ac lite, RB2011 Wi-Fi, mipsbe …): v řízeném režimu nepodporujeme.

Rozpoznání: podle nainstalovaných balíčků, architektury a jména desky (board-name obsahuje
„ax“ / „ac“). Doporučení je jen tam, kde je jisté; jinak „nejde poznat“.
Extra balíček s jinou verzí než routeros RouterOS vypne – i to hlásíme.
"""

from __future__ import annotations

import re
from typing import Any

from .routeros import q

WIFI_PKGS = ("wifi-qcom", "wifi-qcom-ac", "wireless")
DOWNLOAD = "https://download.mikrotik.com/routeros/{v}/{pkg}-{v}-{arch}.npk"
ARCHS = {"arm", "arm64", "mipsbe", "mmips", "smips", "tile", "ppc", "x86"}


def _rows(inv: dict, key: str) -> list[dict]:
    v = inv.get(key)
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def _true(v: Any) -> bool:
    return str(v).lower() in ("true", "yes")


def installed(inv: dict) -> dict[str, dict]:
    return {str(p.get("name")): p for p in _rows(inv, "packages") if p.get("name")}


def _has_radios(inv: dict) -> bool:
    return bool(_rows(inv, "wifi") or _rows(inv, "wireless") or _rows(inv, "wifi_radios"))


def chip(inv: dict) -> str | None:
    """ax / ac / legacy / None (bez rádií nebo nejde poznat) – podle desky a architektury."""
    res = inv.get("resource") if isinstance(inv.get("resource"), dict) else {}
    board = str(res.get("board-name") or "")
    arch = str(res.get("architecture-name") or "")
    if re.search(r"ax", board, re.I):
        return "ax"
    if re.search(r"ac", board, re.I):
        # 802.11ac Wave2 Qualcomm jen na ARM; mips (hAP ac 962, ac lite) umí jen wireless
        return "ac" if arch.startswith("arm") else "legacy"
    if _has_radios(inv) and arch.startswith("mips"):
        return "legacy"
    return None


def wifi_status(inv: dict) -> dict:
    """Stav Wi-Fi ovladače: {chip, installed, required, ok, problem, fix}.
    fix = balíček k instalaci (a co odinstalovat), když je to jisté."""
    pk = installed(inv)
    have = [p for p in WIFI_PKGS if p in pk and not _true(pk[p].get("disabled"))]
    c = chip(inv)
    out: dict[str, Any] = {"chip": c, "installed": have, "required": None, "ok": True, "problem": None, "fix": None}
    if not _has_radios(inv) and not have:
        return out
    required = {"ax": "wifi-qcom", "ac": "wifi-qcom-ac"}.get(c or "")
    out["required"] = required
    if c == "legacy":
        out.update(ok=False, problem="rádio umí jen starý ovladač wireless – v řízeném režimu jen jako switch")
        return out
    if required and required not in have:
        remove = [p for p in have if p != required]
        out.update(ok=False, problem=f"chybí balíček {required}" + (f" (nainstalovaný je {', '.join(remove)})" if remove else ""),
                   fix={"install": required, "remove": remove})
    elif not required and "wireless" in have and not ({"wifi-qcom", "wifi-qcom-ac"} & set(have)):
        out.update(ok=False, problem="nainstalovaný je jen starý ovladač wireless – zkontroluj, zda deska umí wifi-qcom(-ac)")
    if len([p for p in have if p in ("wifi-qcom", "wifi-qcom-ac")]) > 1:
        out.update(ok=False, problem="nainstalované jsou oba ovladače wifi-qcom a wifi-qcom-ac – nech jen jeden")
    return out


def version_mismatch(inv: dict) -> list[str]:
    """Extra balíčky s jinou verzí než routeros (RouterOS je po restartu vypne)."""
    pk = installed(inv)
    base = str((pk.get("routeros") or {}).get("version") or "").split(" ")[0]
    if not base:
        return []
    return sorted(f"{n} {str(p.get('version') or '').split(' ')[0]}" for n, p in pk.items()
                  if n != "routeros" and str(p.get("version") or "").split(" ")[0] not in ("", base))


def switch_body(inv: dict, check_certificate: str = "yes-without-crl") -> str:
    """Příkaz: stáhnout správný Wi-Fi balíček pro aktuální verzi a architekturu,
    naplánovat odinstalaci špatného; instalace proběhne při restartu (tail)."""
    st = wifi_status(inv)
    if not st["fix"]:
        raise ValueError("zařízení nepotřebuje výměnu Wi-Fi balíčku (nebo ji nejde určit)")
    res = inv.get("resource") if isinstance(inv.get("resource"), dict) else {}
    ver = str(res.get("version") or "").split(" ")[0]
    arch = str(res.get("architecture-name") or "")
    if not re.fullmatch(r"\d+\.\d+(\.\d+)?", ver) or arch not in ARCHS:
        raise ValueError("neznámá verze nebo architektura RouterOS")
    pkg = st["fix"]["install"]
    url = DOWNLOAD.format(v=ver, pkg=pkg, arch=arch)
    name = f"{pkg}-{ver}-{arch}.npk"
    lines = [f':log warning {q("mtc: vymena Wi-Fi ovladace na " + pkg)}',
             f"/tool fetch url={q(url)} dst-path={q(name)} check-certificate={check_certificate}",
             ":delay 2s",
             f':if ([:len [/file find name={q(name)}]] = 0) do={{ :error "mtc: balicek se nestahl" }}']
    for r in st["fix"]["remove"]:
        lines.append(f"/system package uninstall {r}")
    return "\n".join(lines) + "\n"


SWITCH_TAIL = ':execute ":delay 5s; /system reboot"\n'
