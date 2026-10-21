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

from . import channels, regional
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


def _applies(s: dict, r: dict, band: str) -> bool:
    """Vysílá se SSID na tomto rádiu? (pásmo, skupina AP)"""
    if band not in s["bands"].split(","):
        return False
    groups = s.get("groups") or []
    return not groups or r.get("group") in groups


def manager(ssids: list[dict], site_radio_list: list[dict], policy: dict, *, host_has_radios: bool) -> dict:
    """Konfigurace CAPsMANu: {script, summary, radios, notes, blockers, config_map, channel_plan}.

    ssids: [{id, ssid, vlan_id, bands, isolation, ft, enabled, groups, ppsk: [{name, vlan_id}]}] – bez hesel.
    Rádia: + group (skupina AP), chip (ax / ac).

    VLAN klientů neřeší datapath (802.11ac ji neumí), ale skript mtc-wifi-vlan na AP podle MAC
    (cap_vlan_script) – stejně pro ax i ac. Více hesel (multi-passphrase) jen na ax (wifi-qcom):
    na ac by se klient s heslem s VLAN nepřipojil, takové SSID se tam nevysílá."""
    blockers: list[str] = []
    notes: list[str] = []
    active = [s for s in ssids if s.get("enabled")]
    if not active:
        blockers.append("žádná zapnutá Wi-Fi síť (SSID)")
    plan = channels.plan(site_radio_list, max_width=int(policy.get("max_width") or 80), dfs=bool(policy.get("dfs", True)))
    by_radio = {(i["device_id"], i["interface"]): i for i in plan["items"]}
    country = policy.get("country") or regional.country()
    lines = [
        "# Wi-Fi lokality - objekty controlleru se nahradi cele",
        '/interface wifi provisioning remove [find comment="mtc"]',
        '/interface wifi configuration remove [find comment="mtc"]',
        '/interface wifi channel remove [find comment="mtc"]',
        ':do { /interface wifi security multi-passphrase remove [find comment="mtc"] } on-error={}',
        '/interface wifi security remove [find comment="mtc"]',
        '/interface wifi datapath remove [find comment="mtc"]',
        '/interface wifi steering remove [find comment="mtc"]',
        # steering (802.11k/v) je doplněk: když ho RouterOS neumí, Wi-Fi musí běžet i bez něj
        f':do {{ /interface wifi steering add name="{STEERING}" rrm=yes wnm=yes comment="mtc" }} '
        'on-error={ :log warning "mtc: steering (802.11k/v) se nepodarilo nastavit" }',
    ]
    names: dict[str, int] = {}
    config_map: dict[str, str] = {}
    for n, s in enumerate(active, 1):
        names[s["id"]] = n
        ft = "ft=yes ft-over-ds=yes" if s.get("ft", True) else "ft=no"
        pp = s.get("ppsk") or []
        if pp:
            # více hesel: jen WPA2-PSK (multi-passphrase WPA3 neumí); hlavní heslo = VLAN SSID
            lines.append(f'/interface wifi security add name="mtc-sec{n}" authentication-types=wpa2-psk '
                         f'passphrase={token(s["id"])} multi-passphrase-group="mtc-mp{n}" {ft} comment="mtc"')
            for k, e in enumerate(pp):
                lines.append(f'/interface wifi security multi-passphrase add group="mtc-mp{n}" '
                             f'passphrase={ppsk_token(s["id"], k)} vlan-id={int(e["vlan_id"])} comment="mtc"')
        else:
            lines.append(f'/interface wifi security add name="mtc-sec{n}" authentication-types=wpa2-psk,wpa3-psk '
                         f'passphrase={token(s["id"])} {ft} management-protection=allowed comment="mtc"')
        # bez bridge a vlan-id: do bridge s VLAN dá rozhraní skript mtc-wifi-vlan na AP (podle MAC)
        lines.append(f'/interface wifi datapath add name="mtc-dp{n}" client-isolation={"yes" if s.get("isolation") else "no"} '
                     'comment="mtc"')
        lines.append(f'/interface wifi configuration add name="mtc-s{n}" ssid={q(s["ssid"])} country={q(country)} '
                     f'mode=ap security="mtc-sec{n}" datapath="mtc-dp{n}" comment="mtc"')
        config_map[f"mtc-s{n}"] = s["id"]
    radio_rows = []
    for r in site_radio_list:
        band = "2.4" if r["band"].startswith("2") else "5"
        here = [s for s in active if _applies(s, r, band)]
        mine = [s for s in here if not s.get("ppsk") or r.get("chip") == "ax"]
        skipped = [s["ssid"] for s in here if s not in mine]
        if skipped:
            notes.append(f"{r['device']} {r['interface']}: {', '.join(skipped)} se nevysílá – více hesel s VLAN "
                         "umí jen ax (wifi-qcom)")
        it = by_radio.get((r["device_id"], r["interface"]))
        mac = r["mac"]
        key = mac.replace(":", "").lower()
        if not mine:
            notes.append(f"{r['device']} {r['interface']}: v pásmu {band} GHz žádné SSID pro tuto skupinu – rádio mlčí")
            continue
        if len(mine) > intent_hard():
            blockers.append(f"{r['device']} {r['interface']}: {len(mine)} SSID v pásmu {band} GHz – nejvýš {intent_hard()}")
        elif len(mine) > 4:
            notes.append(f"{r['device']} {r['interface']}: {len(mine)} SSID v pásmu {band} GHz – nad 4 klesá "
                         "propustnost (každé SSID vysílá beacony)")
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
        config_map[f"mtc-r-{key}"] = first["id"]
        slaves = ",".join(f"mtc-s{names[s['id']]}" for s in rest)
        lines.append(f"/interface wifi provisioning add radio-mac={mac} action=create-dynamic-enabled "
                     f'master-configuration="mtc-r-{key}"' + (f' slave-configurations="{slaves}"' if slaves else "")
                     + ' comment="mtc"')
        radio_rows.append({"device_id": r["device_id"], "device": r["device"], "interface": r["interface"], "band": band,
                           "group": r.get("group"), "chip": r.get("chip"), "ssids": [s["ssid"] for s in mine],
                           "skipped": skipped, "channel": it["new"]["channel"] if it else None, "width": width})
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
            "blockers": blockers, "channel_plan": plan, "config_map": config_map}


def intent_hard() -> int:
    from .intent import HARD_SSIDS_PER_RADIO

    return HARD_SSIDS_PER_RADIO


def ppsk_token(ssid_id: str, idx: int) -> str:
    return f"MTCMP{str(ssid_id).replace('-', '')}X{int(idx)}"


PPSK_RE = re.compile(r"MTCMP([0-9a-f]{32})X(\d{1,5})")


def fill_secrets(script: str, psk_for, ppsk_for=None) -> str:
    """Dosadí hesla za tokeny (při doručení na zařízení).
    psk_for(ssid_hex) → heslo SSID; ppsk_for(ssid_hex, index) → heslo multi-passphrase."""
    def rep(m: re.Match) -> str:
        psk = psk_for(m.group(1))
        if not psk:
            raise ValueError("heslo SSID chybí")
        return q(psk)

    def rep_mp(m: re.Match) -> str:
        psk = ppsk_for(m.group(1), int(m.group(2))) if ppsk_for else None
        if not psk:
            raise ValueError("heslo multi-passphrase chybí")
        return q(psk)

    # jeden průchod: dosazené heslo se už znovu neprohledává (heslo může vypadat jako token)
    return re.sub(f"{TOKEN_RE.pattern}|{PPSK_RE.pattern}",
                  lambda m: rep(TOKEN_RE.fullmatch(m.group(0))) if m.group(1) else rep_mp(PPSK_RE.fullmatch(m.group(0))),
                  script)


# --- VLAN rádií na AP podle MAC (nezávisle na jménech rozhraní) ---------------------------------------

def cap_table(host_inv: dict, ap_inv: dict, config_map: dict[str, str], ssids: dict[str, dict]) -> list[dict]:
    """Rozhraní tohoto AP (podle MAC) → VLAN: [{mac, ssid, pvid, tagged}].

    CAPsMAN (hostitel) ví, kterou konfiguraci (a tím SSID) má každé vzdálené rozhraní;
    AP zná jen MAC. Jména rozhraní na AP se mění (wifi3/wifi4…), MAC ne."""
    macs = {str(w.get("mac-address") or "").upper() for w in _rows(ap_inv, "wifi")} - {""}
    by_ssid_name = {s["ssid"]: sid for sid, s in ssids.items()}
    out: dict[str, dict] = {}
    for w in _rows(host_inv, "wifi"):
        mac = str(w.get("mac-address") or "").upper()
        if mac not in macs:
            continue
        cfg = str(w.get("configuration") or "")
        sid = config_map.get(cfg) or by_ssid_name.get(str(w.get("configuration.ssid") or w.get("ssid") or ""))
        s = ssids.get(sid or "")
        if not s:
            continue
        tagged = sorted({int(e["vlan_id"]) for e in (s.get("ppsk") or [])} - {int(s["vlan_id"])})
        out[mac] = {"mac": mac, "ssid": s["ssid"], "pvid": int(s["vlan_id"]), "tagged": tagged}
    return sorted(out.values(), key=lambda e: e["mac"])


VLAN_SCRIPT = "mtc-wifi-vlan"


def table_sha(table: list[dict]) -> str:
    return hashlib.sha256(json.dumps(table, sort_keys=True).encode()).hexdigest()[:12]


def cap_vlan_source(table: list[dict]) -> str:
    """Zdroj skriptu na AP: každé Wi-Fi rozhraní (najde podle MAC) do bridge s VLAN svého SSID,
    tagovaně do VLAN z více hesel; porty mtc-wifi rozhraní, která nejsou v tabulce, pryč."""
    if any(not re.fullmatch(r"[0-9A-F]{2}(:[0-9A-F]{2}){5}", e["mac"]) for e in table):
        raise ValueError("neplatná MAC")
    pairs = ";".join(f'"{e["mac"]}"="{int(e["pvid"])}|{",".join(str(int(v)) for v in e["tagged"])}"' for e in table)
    arr = f"{{{pairs}}}" if table else '[:toarray ""]'
    return f""":local t {arr}
:local keep [:toarray ""]
:foreach i in=[/interface wifi find] do={{
  :local n [/interface wifi get $i name]
  :local m [/interface wifi get $i mac-address]
  :local e ($t->$m)
  :if ([:len $e] > 0) do={{
    :set keep ($keep, $n)
    :local p [:tonum [:pick $e 0 [:find $e "|"]]]
    :local tg [:pick $e ([:find $e "|"] + 1) [:len $e]]
    :local bp [/interface bridge port find where interface=$n bridge=bridge dynamic=no]
    :if ([:len $bp] = 0) do={{
      :do {{ /interface bridge port add bridge=bridge interface=$n pvid=$p frame-types=admit-all comment="mtc-wifi" }} on-error={{ :log warning ("mtc: " . $n . " nejde do bridge") }}
    }} else={{
      :if ([/interface bridge port get ($bp->0) pvid] != $p) do={{ /interface bridge port set ($bp->0) pvid=$p }}
    }}
    :if ([:len $tg] > 0) do={{
      :foreach v in=[:toarray $tg] do={{
        :local bv [/interface bridge vlan find where bridge=bridge vlan-ids=$v dynamic=no]
        :if ([:len $bv] > 0) do={{
          :local cur [/interface bridge vlan get ($bv->0) tagged]
          :if ([:typeof [:find $cur $n]] = "nil") do={{ /interface bridge vlan set ($bv->0) tagged=($cur, $n) }}
        }}
      }}
    }}
  }}
}}
:foreach b in=[/interface bridge port find where comment="mtc-wifi"] do={{
  :if ([:typeof [:find $keep [/interface bridge port get $b interface]]] = "nil") do={{ /interface bridge port remove $b }}
}}
"""


def cap_vlan_body(table: list[dict]) -> str:
    """Instalace skriptu mtc-wifi-vlan (spouští se po startu a každých 20 s) a první běh."""
    from .routeros import SCRIPT_POLICY, q_source

    sha = table_sha(table)
    return (f'/system scheduler remove [find name="{VLAN_SCRIPT}"]\n'
            f'/system script remove [find name="{VLAN_SCRIPT}"]\n'
            f'/system script add name="{VLAN_SCRIPT}" policy={SCRIPT_POLICY} comment="mtc-wifi:{sha}" '
            f"source={q_source(cap_vlan_source(table))}\n"
            f'/system scheduler add name="{VLAN_SCRIPT}" interval=20s start-date=[/system clock get date] start-time=00:00:00 '
            f'policy={SCRIPT_POLICY} comment="mtc" on-event="/system script run {VLAN_SCRIPT}"\n'
            f"/system script run {VLAN_SCRIPT}\n")


def installed_table_sha(inv: dict) -> str | None:
    for sc in _rows(inv, "scripts"):
        if str(sc.get("name")) == VLAN_SCRIPT:
            c = str(sc.get("comment") or "")
            return c.split(":", 1)[1] if c.startswith("mtc-wifi:") else ""
    return None


def table_basis(inv: dict) -> str:
    """Základ plánu wifitable: otisk nainstalované tabulky (i „nic“) – 64 znaků jako ostatní základy."""
    return hashlib.sha256(str(installed_table_sha(inv)).encode()).hexdigest()


def cap_body(manager_identity: str) -> str:
    """Nastavení CAP na řízeném AP: hledá CAPsMAN na bridgi (management) a drží se jen našeho."""
    return (f"/interface wifi cap set enabled=yes discovery-interfaces=bridge caps-man-names={q(manager_identity)} "
            # slaves-static: virtuální rozhraní SSID zůstanou (stálé MAC) a skript mtc-wifi-vlan je dá do bridge
            "lock-to-caps-man=yes slaves-static=yes\n"
            # fyzická rádia musí řídit CAPsMAN (po resetu bez defconf jsou „local“ a výchozí konfigurace je vypíná)
            ':do { /interface wifi set [find where default-name~"^wifi" and !master-interface] '
            # bez datapath.bridge: dynamický port by nešel nastavit – port v bridgi dělá skript mtc-wifi-vlan
            "configuration.manager=capsman disabled=no } on-error={}\n")


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
