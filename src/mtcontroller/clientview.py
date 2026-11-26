"""Rozbor připojení klienta (Klienti → Rozbor): jedna odpověď pro celou obrazovku.

Skládá, co controller už má: Wi-Fi časovou osu klienta (`wifidiag.client_journey` – AP, signál, přechody,
odpojení), cestu klienta sítí k internetu (`infra.site_view`), stav jeho AP (kanál, počet klientů v čase,
cizí sítě na kanálu) a měření internetu lokality (`wanprobe.series`). Nic nového se ze zařízení neměří –
jen přenesená data klienta se nově ukládají ke vzorkům (`wifi_sample.tx_bytes/rx_bytes`).

Co RouterOS přes API nedává a proto tu není: opakované přenosy (TX retries), vytížení éteru (airtime),
rušení a seznam AP, která klienta „slyší“.
"""

from __future__ import annotations

from typing import Any

from . import channels, clientmeta, infra, inventory, wanprobe, wifidiag
from .db import Tx


def _rows(inv: dict, key: str) -> list[dict]:
    v = inv.get(key)
    return [r for r in v if isinstance(r, dict)] if isinstance(v, list) else []


def activity(samples: list[dict]) -> list[dict]:
    """Čítače od připojení → přenesené bajty mezi sousedními vzorky. Čítač, který klesl, nebo jiné rádio
    znamená nové připojení – počítá se od nuly."""
    out: list[dict] = []
    prev: dict | None = None
    for s in samples:
        if s.get("tx_bytes") is None or s.get("rx_bytes") is None:
            prev = None
            continue
        down, up = int(s["tx_bytes"]), int(s["rx_bytes"])
        same = prev is not None and (prev["device_id"], prev["iface"]) == (s["device_id"], s["iface"]) \
            and down >= prev["tx_bytes"] and up >= prev["rx_bytes"]
        if same:
            out.append({"at": s["at"], "down": down - int(prev["tx_bytes"]), "up": up - int(prev["rx_bytes"])})
        elif prev is not None:
            out.append({"at": s["at"], "down": down, "up": up})
        prev = s
    return out


def _channel(invs: dict[str, dict], cap_of: dict, ap_id: str, radio: str) -> dict:
    """Skutečný kanál rádia: monitor na AP, jinak monitor rozhraní CAP na správci."""
    cands = [(ap_id, radio)] + [k for k, v in cap_of.items() if v["ap_id"] == ap_id and v["radio"] == radio and k[0] != ap_id]
    for did, iface in cands:
        inv = invs.get(did, {})
        for m in _rows(inv, "wifi_monitor"):
            if str(m.get("name")) == iface and m.get("channel"):
                f, w = channels.parse_channel(m.get("channel"))
                if f:
                    return {"freq": f, "channel": channels.chan(f), "width": w,
                            "band": "6" if f >= 5925 else "5" if f >= 4900 else "2.4"}
    return {"freq": None, "channel": None, "width": None, "band": None}


def _path(view: dict, client: dict, wifi: dict | None, probe: dict) -> list[dict]:
    """Klient → jeho zařízení → nadřazená zařízení až k bráně → internet. U každého kroku popis spoje k dalšímu."""
    nodes = {n["id"]: n for n in view.get("nodes", [])}
    hops: list[dict] = [{"kind": "client", "name": client.get("label") or client.get("hostname") or client["mac"],
                         "l1": client["mac"], "l2": client.get("ip") or "", "icon": client.get("icon"),
                         "link": (f"{wifi['signal']} dBm" if wifi and wifi.get("signal") is not None else client.get("port") or ""),
                         "wireless": bool(client.get("ssid"))}]
    cur = nodes.get(str(client.get("device_id") or ""))
    seen: set[str] = set()
    while cur and cur["id"] not in seen and len(hops) < 12:
        seen.add(cur["id"])
        par = nodes.get(str(cur.get("parent") or ""))
        link = ""
        if par:
            link = " → ".join(x for x in (cur.get("uplink_port"), par.get("label") and cur.get("parent_port")) if x)
        hops.append({"kind": "device", "id": cur["id"] if cur.get("role") != "unmanaged" else None, "name": cur.get("label"),
                     "role": cur.get("role"), "state": cur.get("state"), "l1": cur.get("model") or "", "l2": "", "link": link})
        cur = par
    wans = view.get("wans") or []
    w = next((x for x in wans if x.get("active")), wans[0] if wans else None)
    last = next((t for t in probe.get("targets") or [] if t.get("rtt_avg") is not None), None)
    if hops and hops[-1]["kind"] == "device":
        hops[-1]["link"] = f"{round(last['rtt_avg'])} ms" if last else ""
    hops.append({"kind": "wan", "name": "Internet", "l1": (w or {}).get("address") or "", "l2": (w or {}).get("interface") or "",
                 "availability": probe.get("availability"), "link": ""})
    return hops


def insight(tx: Tx, site_id: Any, mac: str, hours: int) -> dict:
    sid = str(site_id)
    j = wifidiag.client_journey(tx, sid, mac, hours)
    c = tx.fetchone(
        """SELECT mac, ip, hostname, device_id, device, port, vlan, ssid, signal, first_seen, last_seen,
                  last_seen > now() - interval '10 minutes' AS online
           FROM client WHERE site_id = %(s)s AND mac = %(m)s""", {"s": sid, "m": mac}) or {"mac": mac}
    client = clientmeta.annotate(tx, [dict(c)])[0]
    if client.get("device_id"):
        client["device_id"] = str(client["device_id"])
    p = {"s": sid, "m": mac, "h": int(hours)}
    raw = tx.fetchall("""SELECT at, device_id, iface, tx_bytes, rx_bytes FROM wifi_sample
                         WHERE site_id = %(s)s AND mac = %(m)s AND at > now() - make_interval(hours => %(h)s::int)
                         ORDER BY at LIMIT 20000""", p)
    act = activity(raw)
    probe = wanprobe.series(tx, sid, hours)
    view = infra.site_view(tx, sid)

    # AP: to, na kterém klient je teď; když není připojený, poslední z časové osy
    lanes = {ln["key"]: ln for ln in j["lanes"]}
    last_seg = j["segments"][-1] if j["segments"] else None
    wifi_now = None
    ap = None
    if client.get("ssid") and client.get("device_id"):
        ap = {"ap_id": client["device_id"], "ap": client.get("device"), "radio": client.get("port") or ""}
        sig = str(client.get("signal") or "")
        last_s = next((s for s in reversed(j["samples"]) if s.get("signal") is not None), None)
        wifi_now = {"signal": int(sig) if sig.lstrip("-").isdigit() else (last_s or {}).get("signal"),
                    "tx_rate": (j["samples"][-1] if j["samples"] else {}).get("tx_rate"),
                    "rx_rate": (j["samples"][-1] if j["samples"] else {}).get("rx_rate"), "ssid": client.get("ssid")}
    elif last_seg and last_seg["lane"] in lanes:
        ln = lanes[last_seg["lane"]]
        ap = {"ap_id": ln["ap_id"], "ap": ln["ap"], "radio": ln["radio"]}
    ap_out = None
    if ap:
        devs = tx.fetchall("""SELECT id, identity, serial FROM device WHERE site_id = %(s)s
                              AND state IN ('adopted', 'offline')""", {"s": sid})
        invs = {str(d["id"]): inventory.load(tx, d["id"], ["wifi", "wifi_radios", "wifi_monitor"]) for d in devs}
        cap_of = wifidiag.iface_map(devs, invs)
        ch = _channel(invs, cap_of, ap["ap_id"], ap["radio"])
        # počet klientů AP v čase: vzorky jsou u toho, kdo drží registrační tabulku (AP nebo CAPsMAN)
        step = 300 if hours <= 48 else 3600
        buckets: dict[Any, set] = {}
        for r in tx.fetchall(
                """SELECT to_timestamp(floor(extract(epoch FROM at) / %(st)s) * %(st)s) AS at, device_id, iface, mac
                   FROM wifi_sample WHERE site_id = %(s)s AND at > now() - make_interval(hours => %(h)s::int)
                   GROUP BY 1, 2, 3, 4""", dict(p, st=step)):
            info = cap_of.get((str(r["device_id"]), r["iface"]))
            owner = info["ap_id"] if info else str(r["device_id"])
            if owner == ap["ap_id"]:
                buckets.setdefault(r["at"], set()).add(r["mac"])
        now_n = tx.fetchone("""SELECT count(*) AS n FROM client WHERE site_id = %(s)s AND device_id = %(d)s
                               AND ssid IS NOT NULL AND last_seen > now() - interval '10 minutes'""",
                            {"s": sid, "d": ap["ap_id"]})["n"]
        foreign = 0
        if ch["freq"]:
            foreign = tx.fetchone("""SELECT count(*) AS n FROM wifi_bss WHERE site_id = %(s)s AND kind <> 'own'
                                     AND frequency = %(f)s AND last_seen > now() - interval '24 hours'""",
                                  {"s": sid, "f": ch["freq"]})["n"]
        others = []
        for key, ln in lanes.items():
            if ln["ap_id"] == ap["ap_id"] and ln["radio"] == ap["radio"]:
                continue
            segs = [s for s in j["segments"] if s["lane"] == key]
            sigs = [x["signal"] for s in segs for x in s["signals"] if x.get("signal") is not None]
            if segs:
                others.append({"ap": ln["ap"], "ap_id": ln["ap_id"], "radio": ln["radio"], "band": ln["band"],
                               "times": len(segs), "last_signal": sigs[-1] if sigs else None})
        ap_out = {"id": ap["ap_id"], "name": ap["ap"], "radio": ap["radio"], **ch, "clients": int(now_n),
                  "foreign": int(foreign), "others": others,
                  "series": [{"at": t, "clients": len(m)} for t, m in sorted(buckets.items())]}
    return {"client": client, "wifi": wifi_now, "path": _path(view, client, wifi_now, probe),
            "journey": j, "activity": act, "ap": ap_out,
            "site": {"points": probe["points"], "availability": probe["availability"], "targets": probe["targets"],
                     "wans": [{"interface": w.get("interface"), "address": w.get("address"), "active": w.get("active")}
                              for w in view.get("wans") or []]},
            "hours": int(hours)}
