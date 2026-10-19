"""Wi-Fi řízené lokality (F5b): CAPsMAN na jednom řízeném zařízení, AP jako CAP.

- Wi-Fi sítě (SSID) jsou navázané na sítě záměru (VLAN); heslo je v DB jen šifrované
  a do skriptu plánu se ukládá jen zástupný token MTCPSK<id>, skutečné heslo se
  dosadí až při doručení na zařízení (services.render_command).
- Manager: objekty `mtc-*` (komentář "mtc") se při každém nasazení nahradí celé –
  změna Wi-Fi nemůže přerušit správu (AP jsou na drátě), proto bez návratu.
- Rádia AP se provisionují podle radio-mac: master konfigurace per rádio
  (kanál z plánu kanálů), další SSID jako slave konfigurace.
- Roaming: 802.11r (FT, FT over DS) – funguje jen mezi AP jedné instance CAPsMANu;
  802.11k/v přes steering (rrm, wnm).

Jen čisté funkce, vše do skriptu přes routeros.q().
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from . import channels
from .routeros import q

TOKEN_RE = re.compile(r"MTCPSK([0-9a-f]{32})")
STEERING = "mtc-steer"


def token(ssid_id: str) -> str:
    return "MTCPSK" + str(ssid_id).replace("-", "")


def _rows(inv: dict, key: str) -> list[dict]:
    v = inv.get(key)
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def _true(v: Any) -> bool:
    return str(v).lower() in ("true", "yes")


def _std(bands: list[str], band: str) -> str:
    """Nejvyšší standard rádia v pásmu: 5ghz-ax > ac > n > a; 2ghz-ax > n > g."""
    prefix = "2ghz" if band == "2.4" else "5ghz"
    order = ["ax", "ac", "n", "a"] if band == "5" else ["ax", "n", "g"]
    have = {b.split("-", 1)[1] for b in bands if b.startswith(prefix + "-")}
    for o in order:
        if o in have:
            return f"{prefix}-{o}"
    return f"{prefix}-n"


def radios(inv: dict) -> list[dict]:
    """Rádia zařízení s novým balíkem wifi: [{interface, mac, band, std}]."""
    out = []
    for r in _rows(inv, "wifi_radios"):
        bands = [b for b in str(r.get("bands") or "").split(",") if b]
        mac = str(r.get("radio-mac") or "").upper()
        if not mac or not bands:
            continue
        band = "5" if any(b.startswith("5ghz") for b in bands) else "2.4" if any(b.startswith("2ghz") for b in bands) else None
        if band:
            out.append({"interface": str(r.get("interface") or ""), "mac": mac, "band": band, "std": _std(bands, band)})
    if out:
        return sorted(out, key=lambda r: r["interface"])
    # starší výpis bez /interface/wifi/radio: jen rádia s pevně známým pásmem
    for w in _rows(inv, "wifi"):
        if w.get("master-interface") or _true(w.get("dynamic")):
            continue
        b = str(w.get("channel.band") or "")
        mac = str(w.get("radio-mac") or w.get("mac-address") or "").upper()
        if mac and b[:1] in ("2", "5"):
            band = "2.4" if b.startswith("2") else "5"
            out.append({"interface": str(w.get("default-name") or w.get("name")), "mac": mac, "band": band,
                        "std": b if re.fullmatch(r"[25]ghz-[a-z]+", b) else _std([], band)})
    return sorted(out, key=lambda r: r["interface"])


def supports_wifi(inv: dict) -> bool | None:
    """True = rádia s balíkem wifi (wifi-qcom / wifi-qcom-ac), False = jen starý wireless, None = bez rádií."""
    if _rows(inv, "wifi") or _rows(inv, "wifi_radios"):
        return True
    if _rows(inv, "wireless"):
        return False
    return None


def _monitor(inv: dict) -> dict[str, dict]:
    return {str(m.get("name")): m for m in _rows(inv, "wifi_monitor")}


def site_radios(aps: list[dict], invs: dict[str, dict]) -> list[dict]:
    """Rádia AP lokality ve tvaru pro channels.plan (+ mac, std)."""
    out = []
    for d in aps:
        inv = invs.get(str(d["id"]), {})
        mon = _monitor(inv)
        clients: dict[str, int] = {}
        for c in _rows(inv, "wifi_clients"):
            clients[str(c.get("interface"))] = clients.get(str(c.get("interface")), 0) + 1
        for r in radios(inv):
            f_now, w_now = channels.parse_channel((mon.get(r["interface"]) or {}).get("channel"))
            out.append({"device_id": str(d["id"]), "device": d.get("identity") or d.get("serial"),
                        "interface": r["interface"], "mac": r["mac"], "std": r["std"],
                        "band": "2ghz" if r["band"] == "2.4" else "5ghz", "freq_now": f_now, "width_now": w_now,
                        "frequency": "auto", "driver": "wifi", "running": True, "disabled": False,
                        "clients": clients.get(r["interface"], 0)})
    return out


def _width_arg(w: int, band: str) -> str:
    if band == "2.4":
        return "20mhz"
    return {20: "20mhz", 40: "20/40mhz", 80: "20/40/80mhz"}[w]


def manager(ssids: list[dict], site_radio_list: list[dict], policy: dict, *, host_has_radios: bool) -> dict:
    """Konfigurace CAPsMANu: {script, summary, items (plán kanálů), notes, blockers}.

    ssids: [{id, ssid, vlan_id, bands, isolation, ft, enabled}] – bez hesel."""
    blockers: list[str] = []
    notes: list[str] = []
    active = [s for s in ssids if s.get("enabled")]
    if not active:
        blockers.append("žádná zapnutá Wi-Fi síť (SSID)")
    for band in ("2.4", "5"):
        n = sum(1 for s in active if band in s["bands"].split(","))
        if n > 4:
            blockers.append(f"v pásmu {band} GHz je {n} SSID – nejvýš 4 (každé SSID vysílá vlastní beacony)")
    plan = channels.plan(site_radio_list, max_width=int(policy.get("max_width") or 80), dfs=bool(policy.get("dfs", True)))
    by_radio = {(i["device_id"], i["interface"]): i for i in plan["items"]}
    country = policy.get("country") or "Czech Republic"
    lines = [
        "# Wi-Fi lokality - objekty controlleru se nahradi cele",
        '/interface wifi provisioning remove [find comment="mtc"]',
        '/interface wifi configuration remove [find comment="mtc"]',
        '/interface wifi channel remove [find comment="mtc"]',
        '/interface wifi security remove [find comment="mtc"]',
        '/interface wifi datapath remove [find comment="mtc"]',
        '/interface wifi steering remove [find comment="mtc"]',
        # steering (802.11k/v) je doplněk: když ho RouterOS neumí, Wi-Fi musí běžet i bez něj
        f':do {{ /interface wifi steering add name="{STEERING}" rrm=yes wnm=yes comment="mtc" }} '
        'on-error={ :log warning "mtc: steering (802.11k/v) se nepodarilo nastavit" }',
    ]
    names: dict[str, int] = {}
    for n, s in enumerate(active, 1):
        names[s["id"]] = n
        ft = "ft=yes ft-over-ds=yes" if s.get("ft", True) else "ft=no"
        lines.append(f'/interface wifi security add name="mtc-sec{n}" authentication-types=wpa2-psk,wpa3-psk '
                     f'passphrase={token(s["id"])} {ft} management-protection=allowed comment="mtc"')
        lines.append(f'/interface wifi datapath add name="mtc-dp{n}" bridge=bridge vlan-id={int(s["vlan_id"])} '
                     f'client-isolation={"yes" if s.get("isolation") else "no"} comment="mtc"')
        lines.append(f'/interface wifi configuration add name="mtc-s{n}" ssid={q(s["ssid"])} country={q(country)} '
                     f'mode=ap security="mtc-sec{n}" datapath="mtc-dp{n}" comment="mtc"')
    radio_rows = []
    for r in site_radio_list:
        band = "2.4" if r["band"].startswith("2") else "5"
        mine = [s for s in active if band in s["bands"].split(",")]
        it = by_radio.get((r["device_id"], r["interface"]))
        mac = r["mac"]
        key = mac.replace(":", "").lower()
        if not mine:
            notes.append(f"{r['device']} {r['interface']}: v pásmu {band} GHz není žádné SSID – rádio zůstane vypnuté")
            continue
        freq, width = (it["new"]["freq"], it["new"]["width"]) if it else (None, 20)
        ch = f'/interface wifi channel add name="mtc-ch-{key}" band={r["std"]} width={_width_arg(width, band)}'
        if freq:
            ch += f" frequency={int(freq)}"
        if band == "5" and not policy.get("dfs", True):
            ch += " skip-dfs-channels=all"
        lines.append(ch + ' comment="mtc"')
        first, rest = mine[0], mine[1:]
        n1 = names[first["id"]]
        lines.append(f'/interface wifi configuration add name="mtc-r-{key}" ssid={q(first["ssid"])} country={q(country)} '
                     f'mode=ap security="mtc-sec{n1}" datapath="mtc-dp{n1}" '
                     f'channel="mtc-ch-{key}" comment="mtc"')
        slaves = ",".join(f"mtc-s{names[s['id']]}" for s in rest)
        lines.append(f"/interface wifi provisioning add radio-mac={mac} action=create-dynamic-enabled "
                     f'master-configuration="mtc-r-{key}"' + (f' slave-configurations="{slaves}"' if slaves else "")
                     + ' comment="mtc"')
        radio_rows.append({"device": r["device"], "interface": r["interface"], "band": band, "ssids": [s["ssid"] for s in mine],
                           "channel": it["new"]["channel"] if it else None, "width": width})
    lines += [
        f':do {{ /interface wifi configuration set [find comment="mtc"] steering="{STEERING}" }} on-error={{}}',
        '/interface wifi capsman set enabled=yes interfaces=bridge upgrade-policy=none require-peer-certificate=no',
    ]
    if host_has_radios:
        # vlastní rádia manageru řídí stejný CAPsMAN (ověřit v labu)
        lines.append(':do { /interface wifi cap set enabled=yes caps-man-addresses=127.0.0.1 lock-to-caps-man=yes } on-error={}')
    lines.append(":do { /interface wifi radio provision [find] } on-error={}")
    if not site_radio_list:
        notes.append("žádné AP s rolí AP + switch – CAPsMAN se nastaví, rádia přibudou po převzetí AP")
    notes += plan.get("notes", [])
    summary = (f"Wi-Fi: {len(active)} SSID ({', '.join(s['ssid'] for s in active)}), {len(radio_rows)} rádií"
               + (f", kolize {plan['before']} → {plan['after']}" if plan.get("before") is not None else ""))
    return {"script": "\n".join(lines) + "\n", "summary": summary, "radios": radio_rows, "notes": notes,
            "blockers": blockers, "channel_plan": plan}


def fill_secrets(script: str, psk_for) -> str:
    """Dosadí hesla za tokeny (při doručení na zařízení). psk_for(ssid_id_hex) → heslo."""
    def rep(m: re.Match) -> str:
        psk = psk_for(m.group(1))
        if not psk:
            raise ValueError("heslo SSID chybí")
        return q(psk)

    return TOKEN_RE.sub(rep, script)


def cap_body(manager_identity: str) -> str:
    """Nastavení CAP na řízeném AP: hledá CAPsMAN na bridgi (management) a drží se jen našeho."""
    return (f"/interface wifi cap set enabled=yes discovery-interfaces=bridge caps-man-names={q(manager_identity)} "
            "lock-to-caps-man=yes slaves-static=no\n"
            # fyzická rádia musí řídit CAPsMAN (po resetu bez defconf jsou „local“ a výchozí konfigurace je vypíná)
            ':do { /interface wifi set [find where default-name~"^wifi" and !master-interface] '
            "configuration.manager=capsman datapath.bridge=bridge disabled=no } on-error={}\n")


def radios_need_capsman(inv: dict) -> bool:
    """Fyzické rádio, které neřídí CAPsMAN (manager local) nebo je vypnuté."""
    for w in _rows(inv, "wifi"):
        if w.get("master-interface") or _true(w.get("dynamic")) or not str(w.get("default-name") or "").startswith("wifi"):
            continue
        if str(w.get("configuration.manager") or "local") not in ("capsman", "capsman-or-local") or _true(w.get("disabled")):
            return True
    return False


def basis(inv: dict) -> str:
    keep = {k: inv.get(k) for k in ("wifi_capsman", "wifi_prov")}
    return hashlib.sha256(json.dumps(keep, sort_keys=True, default=str).encode()).hexdigest()
