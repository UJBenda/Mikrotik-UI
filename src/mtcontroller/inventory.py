"""Inventura zařízení přes REST (jen čtení) a metriky rozhraní.

Sbírá se jen to, co je potřeba pro přehled, analýzu VLAN/NTP a topologii.
Žádná sekce nesmí obsahovat hesla (uživatelé, PPP secrety, wifi klíče se
nečtou). Chybějící menu (např. /system/routerboard na CHR) se přeskočí.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from .db import Database, operator_scope, tenant_scope
from .rosapi import PinMismatch, RosApi, RosApiError

log = logging.getLogger("mtc.inventory")
_HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class Section:
    name: str
    path: str
    proplist: str | None = None
    # filtr REST (?pole=hodnota) – např. jen výchozí routa, ne celá BGP tabulka
    query: tuple[tuple[str, str], ...] = ()


SECTIONS: list[Section] = [
    Section("identity", "/system/identity"),
    Section("resource", "/system/resource"),
    Section("routerboard", "/system/routerboard"),
    Section("clock", "/system/clock"),
    Section("packages", "/system/package", "name,version,disabled,build-time"),
    Section("update", "/system/package/update"),
    Section("interfaces", "/interface",
            "name,type,mac-address,running,disabled,mtu,comment,rx-byte,tx-byte,rx-error,tx-error,"
            "rx-drop,tx-drop,link-downs,last-link-up-time,slave,dynamic"),
    Section("ethernet", "/interface/ethernet", "name,default-name,switch,poe-out,loop-protect,loop-protect-status"),
    Section("switch", "/interface/ethernet/switch", "name,type"),
    Section("bridges", "/interface/bridge",
            "name,vlan-filtering,protocol-mode,pvid,frame-types,ingress-filtering,disabled,comment,priority"),
    Section("bridge_ports", "/interface/bridge/port",
            "bridge,interface,pvid,frame-types,ingress-filtering,hw,disabled,inactive,dynamic,comment,edge"),
    Section("bridge_vlans", "/interface/bridge/vlan",
            "bridge,vlan-ids,tagged,untagged,current-tagged,current-untagged,disabled,dynamic"),
    Section("vlans", "/interface/vlan", "name,vlan-id,interface,disabled,comment"),
    Section("addresses", "/ip/address", "address,interface,network,disabled,dynamic"),
    Section("neighbors", "/ip/neighbor",
            "interface,address,mac-address,identity,platform,board,version,interface-name"),
    Section("ntp_client", "/system/ntp/client"),
    Section("ntp_server", "/system/ntp/server"),
    Section("dhcp_servers", "/ip/dhcp-server", "name,interface,disabled,address-pool"),
    Section("ip_pools", "/ip/pool", "name,ranges,next-pool"),
    # klienti
    Section("dhcp_leases", "/ip/dhcp-server/lease",
            "address,active-address,mac-address,active-mac-address,host-name,server,status,last-seen,dynamic"),
    Section("arp", "/ip/arp", "address,mac-address,interface,dynamic,complete"),
    Section("bridge_hosts", "/interface/bridge/host", "mac-address,on-interface,vid,bridge,local,external,dynamic"),
    Section("wifi_clients", "/interface/wifi/registration-table",
            "interface,mac-address,ssid,signal,tx-rate,rx-rate,uptime,bytes,last-ip"),
    Section("wireless_clients", "/interface/wireless/registration-table",
            "interface,mac-address,signal-strength,tx-rate,rx-rate,uptime,bytes,last-ip"),
    # wifi rádia a CAPsMAN (bez .proplist – ploché klíče "configuration.ssid" apod.;
    # tajemství odstraní _scrub)
    Section("wifi", "/interface/wifi"),
    Section("wireless", "/interface/wireless",
            "name,mac-address,ssid,mode,band,frequency,channel-width,running,disabled,master-interface,"
            "interface-type,comment"),
    # STP stav portů zvlášť (mění se často – nesmí do bridge_ports, ze kterých se počítá basis plánů)
    Section("stp_ports", "/interface/bridge/port",
            "interface,bridge,role,edge-port,point-to-point-port,forwarding,learning,root-path-cost,"
            "designated-bridge,path-cost,inactive,disabled"),
    # bezpečnostní audit (hardening.py) – jen nastavení, žádná tajemství
    Section("ip_services", "/ip/service", "name,port,disabled,address,certificate"),
    Section("users", "/user", "name,group,disabled,address"),
    Section("mac_server", "/tool/mac-server", "allowed-interface-list"),
    Section("mac_winbox", "/tool/mac-server/mac-winbox", "allowed-interface-list"),
    Section("neighbor_discovery", "/ip/neighbor/discovery-settings", "discover-interface-list,protocol"),
    Section("dns", "/ip/dns", "allow-remote-requests"),
    Section("snmp", "/snmp", "enabled"),
    Section("socks", "/ip/socks", "enabled"),
    Section("proxy", "/ip/proxy", "enabled"),
    Section("bw_server", "/tool/bandwidth-server", "enabled,authenticate"),
    Section("romon", "/tool/romon", "enabled"),
    Section("fw_filter", "/ip/firewall/filter",
            "chain,action,disabled,dynamic,in-interface,in-interface-list,out-interface,out-interface-list,"
            "src-address,src-address-list,dst-address,dst-address-list,connection-state,connection-nat-state,"
            "protocol,src-port,dst-port,port,jump-target,log,time,comment,bytes,packets,invalid,ipsec-policy,"
            "connection-mark,packet-mark,routing-mark,src-mac-address,layer7-protocol,content,icmp-options,"
            "tcp-flags,limit,connection-limit,dst-address-type,src-address-type,hotspot,in-bridge-port,out-bridge-port"),
    Section("fw_nat", "/ip/firewall/nat",
            "chain,action,disabled,dynamic,in-interface,in-interface-list,out-interface,out-interface-list,"
            "src-address,src-address-list,dst-address,dst-address-list,protocol,dst-port,to-addresses,to-ports,"
            "comment,bytes,packets,invalid"),
    Section("if_lists", "/interface/list", "name,builtin,include,exclude,comment"),
    Section("if_list_members", "/interface/list/member", "list,interface,disabled,dynamic"),
    # WAN: výchozí routy (jen 0.0.0.0/0) a DHCP klient – pro pohled Infrastruktura
    Section("routes_default", "/ip/route", "dst-address,gateway,immediate-gw,distance,active,disabled,comment,routing-table",
            (("dst-address", "0.0.0.0/0"),)),
    Section("dhcp_client", "/ip/dhcp-client", "interface,address,gateway,status,disabled,add-default-route"),
    Section("capsman_caps", "/interface/wifi/capsman/remote-cap"),
    Section("capsman_caps_legacy", "/caps-man/remote-cap"),
]

# rozhraní, pro která má smysl ukládat čítače (dynamická PPP serverová rozhraní ne)
_SKIP_TYPES = {"pppoe-in", "sstp-in", "l2tp-in", "ovpn-in", "pptp-in", "loopback"}

# klíče, které nikdy nesmí do DB (pojistka, kdyby je RouterOS přidal do výpisu)
_FORBIDDEN_KEYS = {"password", "passphrase", "secret", "private-key", "psk", "authentication-key",
                   "pre-shared-key", "wps"}


def _scrub(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items() if not any(f in k.lower() for f in _FORBIDDEN_KEYS)}
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    return obj


def collect(api: RosApi) -> tuple[dict[str, Any], list[str]]:
    """Načte všechny sekce. Vrací (data, chyby). Chyba pinningu/přihlášení se
    vyhazuje hned – nemá smysl zkoušet další sekce."""
    data: dict[str, Any] = {}
    errors: list[str] = []
    try:
        _collect_into(api, data, errors)
    finally:
        close = getattr(api, "close", None)
        if close:
            close()
    return data, errors


def _fetch(api: RosApi, data: dict[str, Any], errors: list[str], name: str, method: str, path: str,
           params: dict | None = None, body: dict | None = None) -> None:
    try:
        if method == "GET":
            data[name] = _scrub(api.request("GET", path, params=params))
        else:
            data[name] = _scrub(api.request(method, path, body))
    except PinMismatch:
        raise
    except RosApiError as e:
        if e.status in (401, 403):
            raise
        if e.status in (400, 404, 406) or e.status is None and "no such" in str(e):
            return  # menu na tomto zařízení neexistuje
        errors.append(f"{name}: {e}")


# stav linek: monitor je příkaz (POST), "once" = jedno měření, nic nemění
MONITOR_PROPS = "name,status,rate,full-duplex,auto-negotiation,sfp-module-present,sfp-rx-power,sfp-tx-power"
POE_PROPS = "name,poe-out-status,poe-out-voltage,poe-out-current,poe-out-power"


def _collect_into(api: RosApi, data: dict[str, Any], errors: list[str]) -> None:
    for s in SECTIONS:
        params = dict(s.query)
        if s.proplist:
            params[".proplist"] = s.proplist
        _fetch(api, data, errors, s.name, "GET", s.path, params=params or None)
    eth = [e for e in data.get("ethernet") or [] if isinstance(e, dict) and e.get("name")]
    names = [str(e["name"]) for e in eth]
    if names:
        _fetch(api, data, errors, "ethernet_monitor", "POST", "/interface/ethernet/monitor",
               body={"numbers": ",".join(names), "once": "true", ".proplist": MONITOR_PROPS})
    # SNMP komunita je fakticky heslo: do DB jen to, zda je výchozí "public"
    try:
        comms = api.request("GET", "/snmp/community", params={".proplist": "name,addresses,disabled"})
        if isinstance(comms, list):
            data["snmp_communities"] = [
                {"is_public": str(c.get("name")) == "public", "disabled": c.get("disabled"),
                 "open": str(c.get("addresses") or "0.0.0.0/0") in ("0.0.0.0/0", "::/0", "")}
                for c in comms if isinstance(c, dict)]
    except PinMismatch:
        raise
    except RosApiError as e:
        if e.status in (401, 403):
            raise
    stp = [str(b["name"]) for b in data.get("bridges") or []
           if isinstance(b, dict) and b.get("name") and b.get("protocol-mode") not in (None, "", "none")
           and b.get("disabled") != "true"]
    if stp:
        _fetch(api, data, errors, "stp_bridges", "POST", "/interface/bridge/monitor",
               body={"numbers": ",".join(stp), "once": "true"})
        rows = data.get("stp_bridges")
        if isinstance(rows, dict):
            rows = data["stp_bridges"] = [rows]
        # výstup monitoru nemusí obsahovat jméno – odpovídá pořadí v "numbers"
        if isinstance(rows, list) and len(rows) == len(stp):
            for name, r in zip(stp, rows):
                if isinstance(r, dict):
                    r.setdefault("name", name)
    # skutečný kanál rádií (konfigurace bývá „auto“): /interface/wifi/monitor once
    radios = [str(w["name"]) for w in data.get("wifi") or []
              if isinstance(w, dict) and w.get("name") and w.get("disabled") != "true"]
    if radios:
        _fetch(api, data, errors, "wifi_monitor", "POST", "/interface/wifi/monitor",
               body={"numbers": ",".join(radios), "once": "true"})
        rows = data.get("wifi_monitor")
        if isinstance(rows, dict):
            rows = data["wifi_monitor"] = [rows]
        if isinstance(rows, list) and len(rows) == len(radios):
            for name, r in zip(radios, rows):
                if isinstance(r, dict):
                    r.setdefault("name", name)
    poe = [str(e["name"]) for e in eth if e.get("poe-out") not in (None, "", "off")]
    if poe:
        _fetch(api, data, errors, "poe_monitor", "POST", "/interface/ethernet/poe/monitor",
               body={"numbers": ",".join(poe), "once": "true", ".proplist": POE_PROPS})


def basis_sha(data: dict[str, Any], sections: list[str]) -> str:
    """Otisk vybraných sekcí – plán změn vzniká z konkrétního stavu."""
    blob = json.dumps({k: data.get(k) for k in sorted(sections)}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _int(v: Any) -> int | None:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


def interface_samples(data: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for i in data.get("interfaces") or []:
        if not isinstance(i, dict) or i.get("type") in _SKIP_TYPES or i.get("dynamic") == "true":
            continue
        name = str(i.get("name", ""))[:100]
        if not name or name.startswith("<"):
            continue
        out.append({
            "name": name,
            "running": i.get("running") == "true",
            "rx_bytes": _int(i.get("rx-byte")), "tx_bytes": _int(i.get("tx-byte")),
            "rx_errors": _int(i.get("rx-error")), "tx_errors": _int(i.get("tx-error")),
            "rx_drops": _int(i.get("rx-drop")), "tx_drops": _int(i.get("tx-drop")),
            "link_downs": _int(i.get("link-downs")),
        })
    return out


def store(db: Database, tenant_id: Any, device_id: Any, data: dict[str, Any], errors: list[str]) -> None:
    from . import porthistory
    from .hardening import audit as hardening_audit
    from .health import dhcp_pools

    if "dhcp_servers" in data:
        # odvozené při uložení: alerty pak nemusí každou minutu číst všechny leases
        data = dict(data, dhcp_usage=dhcp_pools(data))
    if "ip_services" in data:
        data = dict(data, hardening=hardening_audit(data))
    with tenant_scope(db, tenant_id) as tx:
        if "interfaces" in data:
            # historie portů: porovnání s předchozí inventurou (před přepsáním)
            porthistory.record(tx, tenant_id, device_id, load(tx, device_id, porthistory.KEEP_SECTIONS), data)
        for section, value in data.items():
            tx.execute(
                """INSERT INTO device_inventory (tenant_id, device_id, section, data)
                   VALUES (%(t)s, %(d)s, %(s)s, %(v)s)
                   ON CONFLICT (device_id, section) DO UPDATE SET data = EXCLUDED.data, collected_at = now()""",
                {"t": str(tenant_id), "d": str(device_id), "s": section,
                 "v": value if isinstance(value, (dict, list)) else {"value": value}},
            )
        for smp in interface_samples(data):
            tx.execute(
                """INSERT INTO interface_sample (tenant_id, device_id, name, running, rx_bytes, tx_bytes,
                       rx_errors, tx_errors, rx_drops, tx_drops, link_downs)
                   VALUES (%(t)s, %(d)s, %(name)s, %(running)s, %(rx_bytes)s, %(tx_bytes)s,
                       %(rx_errors)s, %(tx_errors)s, %(rx_drops)s, %(tx_drops)s, %(link_downs)s)""",
                dict(smp, t=str(tenant_id), d=str(device_id)),
            )
        ident = (data.get("identity") or {}).get("name") if isinstance(data.get("identity"), dict) else None
        res = data.get("resource") if isinstance(data.get("resource"), dict) else {}
        tx.execute(
            """UPDATE device SET inventory_at = now(), inventory_error = %(e)s,
                   identity = coalesce(%(i)s, identity),
                   ros_version = coalesce(%(v)s, ros_version),
                   model = coalesce(%(m)s, model)
               WHERE id = %(d)s""",
            {"e": "; ".join(errors)[:1000] or None, "i": (str(ident)[:200] if ident else None),
             "v": (str(res.get("version"))[:50] if res.get("version") else None),
             "m": (str(res.get("board-name"))[:100] if res.get("board-name") else None), "d": str(device_id)},
        )


def store_error(db: Database, tenant_id: Any, device_id: Any, error: str) -> None:
    with tenant_scope(db, tenant_id) as tx:
        tx.execute("UPDATE device SET inventory_at = now(), inventory_error = %(e)s WHERE id = %(d)s",
                   {"e": error[:1000], "d": str(device_id)})


def load(tx, device_id: Any, sections: list[str] | None = None) -> dict[str, Any]:
    if sections:
        rows = tx.fetchall("""SELECT section, data FROM device_inventory WHERE device_id = %(d)s
                              AND section IN (SELECT jsonb_array_elements_text(%(s)s))""",
                           {"d": str(device_id), "s": sections})
    else:
        rows = tx.fetchall("SELECT section, data FROM device_inventory WHERE device_id = %(d)s",
                           {"d": str(device_id)})
    return {r["section"]: r["data"] for r in rows}


ROLLUP_SQL = """
WITH s AS (
  SELECT tenant_id, device_id, name, at,
         rx_bytes - lag(rx_bytes) OVER w AS drx, tx_bytes - lag(tx_bytes) OVER w AS dtx,
         rx_errors - lag(rx_errors) OVER w AS derx, tx_errors - lag(tx_errors) OVER w AS detx,
         rx_drops - lag(rx_drops) OVER w AS ddrx, tx_drops - lag(tx_drops) OVER w AS ddtx,
         link_downs - lag(link_downs) OVER w AS dld,
         extract(epoch FROM at - lag(at) OVER w) AS secs
  FROM interface_sample
  WHERE at >= %(start)s::timestamptz - interval '20 minutes' AND at < %(stop)s::timestamptz
  WINDOW w AS (PARTITION BY device_id, name ORDER BY at)
)
INSERT INTO interface_hourly (tenant_id, device_id, hour, name, rx_bps_avg, tx_bps_avg,
                              rx_bps_max, tx_bps_max, rx_errors, tx_errors, rx_drops, tx_drops,
                              link_downs, samples)
SELECT tenant_id, device_id, date_trunc('hour', at), name,
       avg(drx * 8 / secs) FILTER (WHERE drx >= 0 AND secs > 0)::bigint,
       avg(dtx * 8 / secs) FILTER (WHERE dtx >= 0 AND secs > 0)::bigint,
       max(drx * 8 / secs) FILTER (WHERE drx >= 0 AND secs > 0)::bigint,
       max(dtx * 8 / secs) FILTER (WHERE dtx >= 0 AND secs > 0)::bigint,
       sum(greatest(derx, 0)), sum(greatest(detx, 0)),
       sum(greatest(ddrx, 0)), sum(greatest(ddtx, 0)), sum(greatest(dld, 0)),
       count(*)
FROM s
WHERE at >= %(start)s::timestamptz AND secs IS NOT NULL AND secs < 3600
GROUP BY tenant_id, device_id, date_trunc('hour', at), name
ON CONFLICT (device_id, name, hour) DO UPDATE SET
  rx_bps_avg = EXCLUDED.rx_bps_avg, tx_bps_avg = EXCLUDED.tx_bps_avg,
  rx_bps_max = EXCLUDED.rx_bps_max, tx_bps_max = EXCLUDED.tx_bps_max,
  rx_errors = EXCLUDED.rx_errors, tx_errors = EXCLUDED.tx_errors,
  rx_drops = EXCLUDED.rx_drops, tx_drops = EXCLUDED.tx_drops,
  link_downs = EXCLUDED.link_downs, samples = EXCLUDED.samples
"""


def rollup(db: Database, hours_back: int = 3) -> int:
    """Přepočítá hodinové agregace za posledních `hours_back` hodin (idempotentní)."""
    with operator_scope(db) as tx:
        r = tx.fetchone("SELECT date_trunc('hour', now()) AS stop")
        stop = r["stop"]
        start = tx.fetchone("SELECT %(s)s::timestamptz - make_interval(hours => %(h)s) AS start",
                            {"s": stop, "h": hours_back})["start"]
        # i rozpracovaná aktuální hodina, ať je graf čerstvý
        return tx.execute(ROLLUP_SQL, {"start": start, "stop": stop + _HOUR})


def retention(db: Database, sample_days: int = 14, hourly_days: int = 400, backup_days: int = 400,
              event_days: int = 90) -> None:
    with operator_scope(db) as tx:
        tx.execute("DELETE FROM interface_sample WHERE at < now() - make_interval(days => %(d)s)", {"d": sample_days})
        tx.execute("DELETE FROM interface_hourly WHERE hour < now() - make_interval(days => %(d)s)", {"d": hourly_days})
        tx.execute("DELETE FROM port_event WHERE at < now() - make_interval(days => %(d)s)", {"d": event_days})
        # nejnovější záloha zařízení zůstává vždy
        tx.execute(
            """DELETE FROM device_backup b WHERE taken_at < now() - make_interval(days => %(d)s)
               AND id <> (SELECT max(id) FROM device_backup x WHERE x.device_id = b.device_id)""",
            {"d": backup_days})

