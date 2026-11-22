"""Soused hlášený na VLAN rozhraní (`93-bridge`) se dohledá na fyzický port – mapa, topologie, trunky."""

import copy

from mtcontroller import infra, inventory

GW = {
    "interfaces": [{"name": "ether1", "type": "ether", "mac-address": "AA:00:00:00:00:01"},
                   {"name": "ether2", "type": "ether", "mac-address": "AA:00:00:00:00:02"},
                   {"name": "uplink-sklad", "type": "ether", "mac-address": "AA:00:00:00:00:03"},
                   {"name": "bridge", "type": "bridge", "mac-address": "AA:00:00:00:00:10"},
                   {"name": "93-bridge", "type": "vlan", "mac-address": "AA:00:00:00:00:10"}],
    "ethernet": [{"name": "ether1", "default-name": "ether1"}, {"name": "ether2", "default-name": "ether2"},
                 {"name": "uplink-sklad", "default-name": "ether3"}],
    "bridge_hosts": [{"mac-address": "BB:00:00:00:00:10", "on-interface": "ether2", "vid": "93", "local": "false"},
                     {"mac-address": "CC:00:00:00:00:10", "on-interface": "uplink-sklad", "vid": "93", "local": "false"},
                     {"mac-address": "AA:00:00:00:00:10", "on-interface": "bridge", "local": "true"}],
    "neighbors": [
        {"interface": "93-bridge", "mac-address": "BB:00:00:00:00:10", "identity": "KR-SW-Poe", "discovered-by": "mndp"},
        {"interface": "93-bridge", "mac-address": "CC:00:00:00:00:10", "identity": "KR-AP-VENKU", "discovered-by": "mndp"},
        # týž soused i přímo na portu (LLDP) – po dohledání se nesmí zdvojit
        {"interface": "ether2,bridge", "mac-address": "BB:00:00:00:00:02", "identity": "KR-SW-Poe", "discovered-by": "lldp"},
        # MAC už z tabulky hostů vypadla → port podle jiného záznamu téhož souseda (identita)
        {"interface": "10-bridge", "mac-address": "BB:00:00:00:00:99", "identity": "KR-SW-Poe", "discovered-by": "mndp"},
        # neznámý soused bez stopy zůstane, jak je
        {"interface": "93-bridge", "mac-address": "DD:00:00:00:00:01", "identity": "cizi"},
    ],
}


def test_resolve():
    inv = copy.deepcopy(GW)
    inventory.resolve_neighbor_ports(inv)
    got = [(n["identity"], n["interface"]) for n in inv["neighbors"]]
    assert got == [("KR-SW-Poe", "ether2,93-bridge"), ("KR-AP-VENKU", "uplink-sklad,93-bridge"),
                   ("KR-SW-Poe", "ether2,bridge"), ("KR-SW-Poe", "ether2,10-bridge"), ("cizi", "93-bridge")], got
    assert inv["neighbors"][0]["via"] == "93-bridge"
    again = copy.deepcopy(inv)
    inventory.resolve_neighbor_ports(again)          # podruhé už nic nemění
    assert again["neighbors"] == inv["neighbors"]
    # bez sekce sousedů nebo hostů nic nespadne
    inventory.resolve_neighbor_ports({})
    bare = {"neighbors": copy.deepcopy(GW["neighbors"])}
    inventory.resolve_neighbor_ports(bare)
    assert bare["neighbors"][1]["interface"] == "93-bridge" and bare["neighbors"][3]["interface"] == "ether2,10-bridge"


def _dev(prefix, hosts, neighbors, ports=("ether1",)):
    """Zařízení s VLAN filtrováním: sousedy hlásí jen na VLAN rozhraní `93-bridge`."""
    return {"interfaces": [{"name": p, "type": "ether", "mac-address": f"{prefix}:00:00:00:00:0{i + 2}"} for i, p in enumerate(ports)]
            + [{"name": "bridge", "type": "bridge", "mac-address": f"{prefix}:00:00:00:00:10"},
               {"name": "93-bridge", "type": "vlan", "mac-address": f"{prefix}:00:00:00:00:10"}],
            "ethernet": [{"name": p, "default-name": p} for p in ports],
            "bridge_hosts": [{"mac-address": f"{m}:00:00:00:00:10", "on-interface": port, "vid": "93", "local": "false"}
                             for m, port in hosts.items()],
            "neighbors": [{"interface": "93-bridge", "mac-address": f"{m}:00:00:00:00:10", "identity": ident,
                           "discovered-by": "mndp"} for m, ident in neighbors.items()]}


def _tree(invs):
    names = {"g": "GW", "s": "KR-SW-Poe", "a": "KR-AP-VENKU"}
    devs = [{"id": k, "identity": names[k], "serial": k, "model": "m", "role": "gateway" if k == "g" else "switch", "state": "online",
             "ros_version": "7"} for k in invs]
    return {n["id"]: (n["parent"], n["parent_port"]) for n in infra.site_infra(devs, invs)["nodes"]}


def test_map_shows_physical_port():
    """Mapa lokality: MNDP přes VLAN vidí všechna zařízení VLAN jako sousedy na `93-bridge`. Bez dohledání
    portu z toho vyjde jeden „nespravovaný switch“ a popisky VLAN; s ním skutečný strom s fyzickými porty."""
    def chain():
        # GW ether2 — ether1 switch ether5 — ether1 AP
        return {"g": _dev("AA", {"BB": "ether2", "CC": "ether2"}, {"BB": "KR-SW-Poe", "CC": "KR-AP-VENKU"}, ("ether1", "ether2")),
                "s": _dev("BB", {"AA": "ether1", "CC": "ether5"}, {"AA": "GW", "CC": "KR-AP-VENKU"}, ("ether1", "ether5")),
                "a": _dev("CC", {"AA": "ether1", "BB": "ether1"}, {"AA": "GW", "BB": "KR-SW-Poe"})}
    want = {"g": (None, None), "s": ("g", "ether2"), "a": ("s", "ether5")}
    # i bez dohledání portu u sousedů: strom se složí z tabulek MAC adres (FDB)
    assert _tree(chain()) == want
    # bez FDB a bez dohledání není z čeho strom složit: zařízení zůstanou pod bránou, ale port je
    # „neznámý“ – jméno VLAN rozhraní se jako port neukáže
    bare = chain()
    for inv in bare.values():
        inv["bridge_hosts"] = []
    assert _tree(bare) == {"g": (None, None), "s": ("g", None), "a": ("g", None)}
    invs = chain()
    for inv in invs.values():
        inventory.resolve_neighbor_ports(inv)
    assert _tree(invs) == {"g": (None, None), "s": ("g", "ether2"), "a": ("s", "ether5")}
    # hvězda: obě zařízení přímo na bráně, každé na svém portu (i přejmenovaném)
    gw = copy.deepcopy(GW)
    star = {"g": gw, "s": _dev("BB", {"AA": "ether1", "CC": "ether1"}, {"AA": "GW", "CC": "KR-AP-VENKU"}),
            "a": _dev("CC", {"AA": "ether1", "BB": "ether1"}, {"AA": "GW", "BB": "KR-SW-Poe"})}
    for inv in star.values():
        inventory.resolve_neighbor_ports(inv)
    assert _tree(star) == {"g": (None, None), "s": ("g", "ether2"), "a": ("g", "uplink-sklad")}


def test_load_resolves(tmp_path=None):
    from helpers import drop_db
    from test_modules import _adopted, _env

    from mtcontroller.db import tenant_scope

    db, name, settings, box, app, tid, sid = _env()
    try:
        d = _adopted(db, box, tid, sid, "GW1", "10.200.1.1", "GW")
        inventory.store(db, tid, d, copy.deepcopy(GW), [])
        with tenant_scope(db, tid) as tx:
            inv = inventory.load(tx, d, ["neighbors"])          # pomocné sekce si načte samo a nevrací je
            assert set(inv) == {"neighbors"} and inv["neighbors"][1]["interface"] == "uplink-sklad,93-bridge"
            assert inventory.load(tx, d)["neighbors"][0]["interface"] == "ether2,93-bridge"
    finally:
        drop_db(name)


def test_cascade_from_fdb():
    """Kaskáda brána → switch → spoj (rádio) → spoj → switch → AP jen z tabulek MAC adres; discovery hlásí
    všechny na VLAN rozhraní, nebo vůbec nic. Porty jsou známé na obou koncích každého spoje."""
    chainv = ["gw", "sw1", "ptpa", "ptpb", "sw2", "ap"]
    up = {"sw1": "ether1", "ptpa": "ether1", "ptpb": "wlan1", "sw2": "sfp1", "ap": "ether1"}      # port k bráně
    down = {"gw": "ether3", "sw1": "ether10", "ptpa": "wlan1", "ptpb": "ether1", "sw2": "ether7"}  # port od brány
    mac = lambda d, n: f"{chainv.index(d) + 10:02X}:00:00:00:00:{n:02X}"  # noqa: E731

    def build(discovery):
        invs = {}
        for i, d in enumerate(chainv):
            names = [x for x in (up.get(d), down.get(d)) if x]
            hosts = [{"mac-address": mac(o, 1), "on-interface": up[d] if chainv.index(o) < i else down[d], "vid": "93",
                      "local": "false"} for o in chainv if o != d]
            hosts.append({"mac-address": mac(d, 1), "on-interface": "bridge", "local": "true"})
            invs[d] = {"interfaces": [{"name": n, "type": "wlan" if n.startswith("wlan") else "ether", "mac-address": mac(d, 2 + k)}
                                      for k, n in enumerate(names)]
                       + [{"name": "bridge", "type": "bridge", "mac-address": mac(d, 1)},
                          {"name": "93-bridge", "type": "vlan", "mac-address": mac(d, 1)}],
                       "ethernet": [{"name": n, "default-name": n} for n in names if not n.startswith("wlan")],
                       "bridge_hosts": hosts,
                       "neighbors": [{"interface": "93-bridge", "mac-address": mac(o, 1), "identity": o, "discovery-by": "mndp"}
                                     for o in chainv if o != d] if discovery else []}
        return invs

    devs = [{"id": d, "identity": d, "serial": d, "model": "m", "role": "gateway" if d == "gw" else "switch", "state": "online",
             "ros_version": "7"} for d in chainv]
    want = {"gw": (None, None, None), "sw1": ("gw", "ether3", "ether1"), "ptpa": ("sw1", "ether10", "ether1"),
            "ptpb": ("ptpa", "wlan1", "wlan1"), "sw2": ("ptpb", "ether1", "sfp1"), "ap": ("sw2", "ether7", "ether1")}
    for discovery in (True, False):
        invs = build(discovery)
        for inv in invs.values():
            inventory.resolve_neighbor_ports(inv)
        out = infra.site_infra(devs, invs)
        got = {n["id"]: (n["parent"], n["parent_port"], n["uplink_port"]) for n in out["nodes"]}
        assert got == want, (discovery, got)
        assert [n["level"] for n in out["nodes"]] == [0, 1, 2, 3, 4, 5]
    # neúplné tabulky: koncové AP nezná nikoho kromě brány a horní switch nezná AP – strom drží
    invs = build(False)
    invs["ap"]["bridge_hosts"] = [h for h in invs["ap"]["bridge_hosts"] if h["mac-address"] in (mac("gw", 1), mac("ap", 1))]
    invs["sw1"]["bridge_hosts"] = [h for h in invs["sw1"]["bridge_hosts"] if h["mac-address"] != mac("ap", 1)]
    got = {n["id"]: (n["parent"], n["parent_port"], n["uplink_port"]) for n in infra.site_infra(devs, invs)["nodes"]}
    assert got == want, got
