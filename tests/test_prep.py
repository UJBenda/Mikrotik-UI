"""Příprava zařízení před nasazením: validace návrhu, model portů a VLAN, skript, API a párování při ohlášení."""
import re

import pytest
from test_routeros import OPTS, _balanced

from mtcontroller import prep
from mtcontroller.services import ServiceError

CTX = {"networks": [{"vid": 5, "name": "Kamery"}, {"vid": 10, "name": "Kancelar"}, {"vid": 93, "name": "Sprava"}],
       "mgmt_vlan": 93, "mgmt_native": False, "models": [], "profiles": []}
PORTS = prep.BUILTIN["CRS328-24P-4S+"]["ports"]


def plan(**over):
    d = {"name": "sw-sklad-2", "model": "CRS328-24P-4S+RM", "port_names": PORTS, "poe_ports": PORTS[:24],
         "ports": {**{f"ether{i}": {"mode": "hybrid", "vlan": 10, "tagged": [5], "edge": True} for i in range(1, 11)},
                   "ether11": {"mode": "access", "vlan": 5, "edge": False, "poe": "off", "comment": "Kamera vjezd"},
                   "ether23": {"mode": "access", "vlan": 93},
                   "ether24": {"mode": "trunk"}},
         "mgmt": {"vid": 93, "native": False, "mode": "dhcp"}, "unset": "off"}
    d.update(over)
    return d


def test_validate():
    p = prep.validate(plan())
    assert p["ports"]["ether1"] == {"mode": "hybrid", "vlan": 10, "tagged": [5], "edge": True}
    assert p["ports"]["ether11"] == {"mode": "access", "vlan": 5, "edge": False, "poe": "off", "comment": "Kamera vjezd"}
    assert p["ports"]["ether24"] == {"mode": "trunk", "edge": False} and p["serial"] is None and p["unset"] == "off"
    # hybrid bez tagovaných VLAN je obyčejný přístupový port; PoE jen na portech, které ho umí
    q = prep.validate(plan(ports={"ether1": {"mode": "hybrid", "vlan": 10, "tagged": [10]},
                                  "sfp-sfpplus1": {"mode": "access", "vlan": 10, "poe": "off"}, "ether24": {"mode": "trunk"}}))
    assert q["ports"]["ether1"]["mode"] == "access" and "poe" not in q["ports"]["sfp-sfpplus1"]
    st = prep.validate(plan(serial="hgb09abc12", mgmt={"vid": 93, "mode": "static", "address": "10.9.3.20/24",
                                                       "gateway": "10.9.3.1", "dns": ["10.9.3.1"]}))
    assert st["serial"] == "HGB09ABC12" and st["mgmt"]["address"] == "10.9.3.20/24" and st["mgmt"]["native"] is True
    for bad in (plan(name="sw sklad"), plan(name=""), plan(model=""), plan(port_names=[]), plan(port_names=["ether1", "ether1"]),
                plan(port_names=['ether1"; /system reset']), plan(serial="x"),
                plan(ports={"ether99": {"mode": "trunk"}}), plan(ports={"ether1": {"mode": "bridge"}}),
                plan(ports={"ether1": {"mode": "access", "vlan": 5000}}), plan(ports={"ether1": {"mode": "access"}}),
                plan(ports={"ether1": {"mode": "access", "vlan": 10, "poe": "boost"}}),
                plan(mgmt={"vid": 0}), plan(mgmt={"vid": 93, "mode": "static", "address": "10.9.3.20", "gateway": "10.9.3.1", "dns": ["1.1.1.1"]}),
                plan(mgmt={"vid": 93, "mode": "static", "address": "10.9.3.20/24", "gateway": "10.9.4.1", "dns": ["1.1.1.1"]}),
                plan(mgmt={"vid": 93, "mode": "static", "address": "10.9.3.20/24", "gateway": "10.9.3.1", "dns": []}),
                plan(unset="default")):
        with pytest.raises(ServiceError):
            prep.validate(bad)


def test_build_and_script():
    p = prep.validate(plan())
    m = prep.build(p, CTX)
    assert m["blockers"] == [] and m["carriers"] == ["ether23", "ether24"]
    by = {it["port"]: it for it in m["ports"]}
    assert (by["ether1"]["mode"], by["ether1"]["pvid"], by["ether1"]["frame_types"]) == ("hybrid", 10, "admit-all")
    assert by["ether24"]["frame_types"] == "admit-only-vlan-tagged" and by["ether24"]["pvid"] == 1 and by["ether24"]["trusted"]
    assert by["ether12"]["mode"] == "off" and by["sfp-sfpplus1"]["mode"] == "off"
    v = {x["vid"]: x for x in m["vlans"]}
    assert v[10]["untagged"] == [f"ether{i}" for i in range(1, 11)] and v[10]["tagged"] == ["ether24"]
    assert v[5]["tagged"] == [f"ether{i}" for i in range(1, 11)] + ["ether24"] and v[5]["untagged"] == ["ether11"]
    assert v[93]["untagged"] == ["ether23"] and v[93]["tagged"] == ["bridge", "ether24"]      # správa: bridge v VLAN tagovaný
    assert any("vypnutých" in w for w in m["warnings"]) and any("DHCP" in w for w in m["warnings"])

    s = prep.script(p, CTX, "https://c.example/enroll/KEY?role=switch", OPTS)
    assert _balanced(s), s
    s.encode("ascii")
    assert s.startswith("# MikroTik controller - pripravena konfigurace sw-sklad-2") and s.rstrip().endswith("}")
    assert 'interface="ether1" pvid=10 frame-types=admit-all' in s and 'interface="ether11" pvid=5' in s
    assert 'vlan-ids=5 tagged="ether1,ether2,' in s and 'comment="mtc:v93"' in s
    assert s.index("vlan-filtering=yes") > s.index('comment="mtc:v93"')              # filtrování až s hotovou tabulkou
    assert '/interface ethernet set [find default-name="ether12"] disabled=yes' in s
    assert '[find bridge=bridge interface="ether1"] edge=yes' in s                    # hybrid s edge
    assert '[find bridge=bridge interface="ether11"] edge=auto' in s                  # přístupový bez edge
    assert '[find bridge=bridge interface="ether24"] edge' not in s                   # trunk: výchozí auto
    assert 'poe set [find default-name="ether11"] poe-out=off' in s
    assert 'comment="mtc Kamera vjezd"' in s and '/system identity set name="sw-sklad-2"' in s
    # správa na VLAN rozhraní nad bridgem (návod MikroTiku „Basic VLAN switching“): bridge jen tagovaný, pvid 1
    assert '/interface bridge add name=bridge vlan-filtering=no protocol-mode=rstp pvid=1 frame-types=admit-only-vlan-tagged' in s
    assert s.index('/interface vlan add name="vlan93-mgmt" interface=bridge vlan-id=93') < s.index("/interface bridge set bridge vlan-filtering=yes")
    assert '/ip dhcp-client add interface="vlan93-mgmt"' in s and "dhcp-client add interface=bridge" not in s
    assert 'comment="mtc-default:switch"' in s
    assert 'name="mtc-enroll"' in s and "c.example/enroll/KEY" in s

    # nativní trunk: VLAN správy jde trunkem bez tagu; pevná adresa
    n = prep.validate(plan(mgmt={"vid": 93, "native": True, "mode": "static", "address": "10.9.3.20/24",
                                 "gateway": "10.9.3.1", "dns": ["10.9.3.1", "1.1.1.1"]}))
    mn = prep.build(n, CTX)
    t = {it["port"]: it for it in mn["ports"]}["ether24"]
    assert t["pvid"] == 93 and t["frame_types"] == "admit-all"
    assert {x["vid"]: x for x in mn["vlans"]}[93]["untagged"] == ["ether23", "ether24"]
    assert {x["vid"]: x for x in mn["vlans"]}[93]["tagged"] == ["bridge"]
    sn = prep.script(n, CTX, "https://c.example/enroll/KEY?role=switch", OPTS)
    assert '/ip address add address="10.9.3.20/24" interface="vlan93-mgmt"' in sn and 'gateway="10.9.3.1"' in sn
    assert '/ip dns set servers="10.9.3.1,1.1.1.1"' in sn and "dhcp-client" not in sn

    # nenastavené porty do výchozí VLAN místo vypnutí
    d = prep.validate(plan(unset="default", unset_vlan=10))
    md = prep.build(d, CTX)
    assert {it["port"]: it for it in md["ports"]}["ether12"]["pvid"] == 10 and "ether12" in {x["vid"]: x for x in md["vlans"]}[10]["untagged"]

    # správa bez cesty ven a VLAN mimo lokalitu = nejde vygenerovat
    b = prep.validate(plan(ports={"ether1": {"mode": "access", "vlan": 10}, "ether2": {"mode": "access", "vlan": 77}}))
    mb = prep.build(b, CTX)
    assert any("nevede ven" in x for x in mb["blockers"]) and any("VLAN 77" in x for x in mb["blockers"])
    with pytest.raises(ServiceError):
        prep.script(b, CTX, "https://c/enroll/K?role=switch", OPTS)
    ok = prep.validate(plan(ports={"ether2": {"mode": "access", "vlan": 77}, "ether24": {"mode": "trunk"}},
                            extra_vlans=[{"vid": 77, "name": "Dílna"}]))
    assert prep.build(ok, CTX)["blockers"] == []
    # souhrn: skupiny portů se stejným nastavením
    sm = prep.summary(p, m)
    g = next(x for x in sm if x["mode"] == "hybrid")
    assert g["ports"] == [f"ether{i}" for i in range(1, 11)] and g["vlan"] == 10 and g["tagged"] == [5] and g["edge"]
    assert next(x for x in sm if x["mode"] == "off")["ports"][0] == "ether12"
    assert prep.model_key("CRS328-24P-4S+RM") == "CRS328-24P-4S+" and prep.model_key("hAP ax^2") == "hAP ax^2"


def test_api_and_pairing():
    from helpers import AsgiClient, drop_db
    from test_flow import _login
    from test_modules import _adopted, _env

    from mtcontroller import inventory, services
    from mtcontroller.db import operator_scope

    db, name, settings, box, app, tid, sid = _env()
    try:
        sw = _adopted(db, box, tid, sid, "SW1", "10.200.1.2", "sw-hala")
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET model = 'CRS326-24G-2S+RM' WHERE id = %(d)s", {"d": sw})
        inventory.store(db, tid, sw, {
            "ethernet": [{"name": f"ether{i}", "default-name": f"ether{i}"} for i in range(1, 9)]
                        + [{"name": "sfp-sfpplus1", "default-name": "sfp-sfpplus1"}],
            "bridge_vlans": [{"bridge": "bridge", "vlan-ids": "93", "tagged": "ether8"}, {"bridge": "bridge", "vlan-ids": "10,5"}],
            "addresses": [{"address": "10.9.3.2/24", "interface": "bridge", "network": "10.9.3.0"}]}, [])
        # profil „všechny VLAN lokality“ (tagged = '*') nesmí průvodce shodit – je to trunk
        from mtcontroller.db import tenant_scope
        with tenant_scope(db, tid) as tx:
            tx.execute("INSERT INTO port_profile (tenant_id, name, pvid, tagged) VALUES (%(t)s, 'Trunk', NULL, '*'), (%(t)s, 'Telefon', 5, '10')",
                       {"t": tid})
        c = _login(app)
        base = f"/api/admin/tenants/{tid}/sites/{sid}/prep"
        r = c.get(base).json()
        assert r["plans"] == []
        # neošetřená chyba: žádné holé 500 – JSON s kódem, podle kterého se dá dohledat v logu; text výjimky ven nejde
        import logging
        seen = []
        h = logging.Handler(); h.emit = lambda rec: seen.append(rec.getMessage())
        logging.getLogger("mtc.web").addHandler(h)
        orig = prep.context
        prep.context = lambda *a: (_ for _ in ()).throw(ValueError("tajne-heslo"))
        try:
            e = c.get(base)
        finally:
            prep.context = orig
            logging.getLogger("mtc.web").removeHandler(h)
        assert e.status_code == 500 and e.json()["kind"] == "internal" and re.fullmatch(r"[0-9a-f]{8}", e.json()["error_id"])
        assert e.json()["error_id"] in e.json()["detail"] and "tajne-heslo" not in e.text
        assert any(e.json()["error_id"] in m and "ValueError" in m and "/prep" in m for m in seen)
        prof = {x["name"]: x for x in r["context"]["profiles"]}
        assert prof["Trunk"]["trunk"] is True and prof["Trunk"]["tagged"] == []
        assert prof["Telefon"]["trunk"] is False and prof["Telefon"]["tagged"] == [10]
        vids = {n["vid"] for n in r["context"]["networks"]}
        assert {5, 10, 93} <= vids                                             # i VLAN bez adresy jdou vybrat
        models = {m["model"]: m for m in r["context"]["models"]}
        assert models["CRS326-24G-2S+"]["source"] == "device" and len(models["CRS326-24G-2S+"]["ports"]) == 9   # ze zařízení, ne vestavěné
        assert models["CRS328-24P-4S+"]["source"] == "builtin" and len(models["CRS328-24P-4S+"]["poe"]) == 24

        bad = c.post(base, json_body=plan(name="má mezeru"))
        assert bad.status_code == 400
        r = c.post(base, json_body=plan(ports={"ether1": {"mode": "access", "vlan": 10}}))
        assert r.status_code == 200 and r.json()["status"] == "draft" and r.json()["blockers"]     # správa nevede ven
        pid = r.json()["id"]
        rv = c.get(f"{base}/{pid}/review").json()
        assert rv["script"] is None and rv["one_liner"] is None and rv["blockers"]
        assert c.get(f"{base}/{pid}/file").status_code == 400
        assert c.post(base, json_body=plan()).status_code == 400                # stejné jméno v lokalitě
        r = c.put(f"{base}/{pid}", json_body=plan(serial="HGB09ABC12"))
        assert r.status_code == 200 and r.json()["status"] == "ready" and r.json()["blockers"] == []
        rv = c.get(f"{base}/{pid}/review").json()
        assert rv["script"].startswith("# MikroTik controller - pripravena konfigurace sw-sklad-2")
        assert f"/enroll/" in rv["one_liner"] and f"/prep/{pid}" in rv["one_liner"] and "reset-configuration" in rv["one_liner"]
        assert rv["vlan_names"]["10"] and rv["carriers"] == ["ether23", "ether24"] and len(rv["summary"]) >= 4
        stage = lambda: {x["id"]: x for x in c.get(base).json()["plans"]}[pid]["stage"]
        assert stage() == "ready" and c.get(f"{base}/{pid}/compare").status_code == 400   # ještě není s čím porovnat
        f = c.get(f"{base}/{pid}/file")
        assert stage() == "fetched"
        assert f.status_code == 200 and "mtc-sw-sklad-2.rsc" in f.headers["content-disposition"] and "vlan-filtering=yes" in f.text
        # jednořádkový příkaz si skript stahuje klíčem lokality – jen návrh z té lokality
        key = re.search(r"/enroll/([^/?\"]+)/prep/", rv["one_liner"]).group(1)
        dev = AsgiClient(app, "checkin.test")
        g = dev.get(f"/enroll/{key}/prep/{pid}")
        assert g.status_code == 200 and 'pvid=10 frame-types=admit-all' in g.text
        assert dev.get(f"/enroll/spatny-klic/prep/{pid}").status_code == 404
        assert dev.get(f"/enroll/{key}/prep/999999").status_code == 404
        with operator_scope(db) as tx:
            assert tx.fetchone("SELECT count(*) AS n FROM audit_log WHERE action IN ('prep.create', 'prep.update')")["n"] == 2

        # zařízení se ohlásí: jiný kus téhož modelu se nespáruje (návrh má sériové číslo), správný ano
        ident = {"model": "CRS328-24P-4S+RM", "version": "7.20.4", "mac": "AA:BB:CC:00:11:22", "role": "switch"}
        other = services.register_device(db, box, "", dict(ident, serial="HGB09ZZZ99"), "192.0.2.9", key)
        reg = services.register_device(db, box, "", dict(ident, serial="HGB09ABC12", mac="AA:BB:CC:00:11:23"), "192.0.2.9", key)
        got = {p["id"]: p for p in c.get(base).json()["plans"]}[pid]
        assert got["status"] == "deployed" and got["device_id"] == str(reg.device_id) != str(other.device_id)
        assert c.put(f"{base}/{pid}", json_body=plan()).status_code == 400      # nasazený návrh už se nemění
        # fáze: ohlásilo se → po schválení „adopted“; porovnání návrhu se skutečností z inventury
        assert got["stage"] == "checked_in"
        assert c.get(f"{base}/{pid}/compare").json() == {"known": False, "diffs": [], "checked": 0}   # bez inventury
        pl = plan(serial="HGB09ABC12")
        bp = [{"bridge": "bridge", "interface": f"ether{i}", "pvid": "10"} for i in range(1, 11)]
        bp += [{"bridge": "bridge", "interface": "ether11", "pvid": "5"}, {"bridge": "bridge", "interface": "ether23", "pvid": "93"},
               {"bridge": "bridge", "interface": "ether24", "pvid": "1"}]
        vl = [{"bridge": "bridge", "vlan-ids": "5", "tagged": ",".join(f"ether{i}" for i in range(1, 11)) + ",ether24"},
              {"bridge": "bridge", "vlan-ids": "10", "tagged": "ether24"}, {"bridge": "bridge", "vlan-ids": "93", "tagged": "ether24"}]
        eth = [{"name": x, "default-name": x, "disabled": "false" if x in pl["ports"] else "true"} for x in PORTS]
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET identity = 'sw-sklad-2' WHERE id = %(d)s", {"d": str(reg.device_id)})
        inventory.store(db, tid, reg.device_id, {"bridge_ports": bp, "bridge_vlans": vl, "ethernet": eth}, [])
        cmp_ = c.get(f"{base}/{pid}/compare").json()
        assert cmp_["known"] and cmp_["checked"] == 28 and cmp_["diffs"] == [], cmp_
        # někdo port přehodí do jiné VLAN, další zapne a přejmenuje zařízení → tři rozdíly
        bp[0]["pvid"] = "5"; eth[14]["disabled"] = "false"
        with operator_scope(db) as tx:
            tx.execute("UPDATE device SET identity = 'jinak' WHERE id = %(d)s", {"d": str(reg.device_id)})
        inventory.store(db, tid, reg.device_id, {"bridge_ports": bp, "bridge_vlans": vl, "ethernet": eth}, [])
        d = {(x["port"], x["what"]): x for x in c.get(f"{base}/{pid}/compare").json()["diffs"]}
        assert d[("ether1", "pvid")]["want"] == 10 and d[("ether1", "pvid")]["have"] == 5
        assert d[("ether15", "disabled")]["want"] is True and d[("", "identity")]["have"] == "jinak" and len(d) == 3, d
        with operator_scope(db) as tx:
            a = tx.fetchone("""SELECT after FROM audit_log WHERE action = 'device.register' AND target = %(t)s""",
                            {"t": f"device:{reg.device_id}"})["after"]
        assert a["prep"]["name"] == "sw-sklad-2"
        # bez sériového čísla: páruje se podle modelu, jen když je volný návrh právě jeden
        p2 = c.post(base, json_body=plan(name="sw-sklad-3")).json()["id"]
        r3 = services.register_device(db, box, "", dict(ident, serial="HGB09QQQ11", mac="AA:BB:CC:00:11:24"), "192.0.2.9", key)
        assert {p["id"]: p for p in c.get(base).json()["plans"]}[p2]["device_id"] == str(r3.device_id)
        p3 = c.post(base, json_body=plan(name="sw-sklad-4")).json()["id"]
        assert c.delete(f"{base}/{p3}").status_code == 200 and c.delete(f"{base}/{p3}").status_code == 404
        assert c.get(f"/api/admin/tenants/{tid}/sites/00000000-0000-0000-0000-000000000000/prep").status_code == 404
    finally:
        drop_db(name)


def test_mstp_and_mgmt_guess():
    ports = [f"ether{i}" for i in range(1, 5)]
    ctx = {"networks": [{"vid": v, "name": f"N{v}", "purpose": "lan", "subnet": None, "gateway": None, "dns": []} for v in (90, 93)],
           "mgmt_vlan": 93, "mgmt_native": False, "managed": False, "models": [], "profiles": []}
    base = {"name": "T", "model": "X", "port_names": ports, "poe_ports": [], "unset": "off",
            "ports": {"ether1": {"mode": "access", "vlan": 90, "edge": True}, "ether4": {"mode": "trunk"}},
            "mgmt": {"vid": 93, "native": False, "mode": "dhcp"}}
    plan = prep.validate({**base, "stp": {"mode": "mstp", "region": "FIRMA", "revision": 3}})
    s = prep.script(plan, ctx, "https://c.example/enroll/KEY", OPTS)
    assert '/interface bridge set bridge protocol-mode=mstp region-name="FIRMA" region-revision=3' in s
    assert _balanced(s) and s.isascii()
    assert any("MSTI" in w for w in prep.build(plan, ctx)["warnings"])
    assert "protocol-mode=mstp" not in prep.script(prep.validate(base), ctx, "https://c.example/enroll/KEY", OPTS)   # výchozí RSTP
    with pytest.raises(Exception):
        prep.validate({**base, "stp": {"mode": "mstp", "region": "má mezeru"}})
    # co má lokalita: převažuje MSTP → nabídne se s regionem
    invs = {"a": {"bridges": [{"name": "bridge", "protocol-mode": "mstp", "region-name": "FIRMA", "region-revision": "3"}]},
            "b": {"bridges": [{"name": "bridge", "protocol-mode": "mstp"}]}, "c": {"bridges": [{"name": "b", "protocol-mode": "rstp"}]}}
    assert prep._site_stp(invs) == {"mode": "mstp", "region": "FIRMA", "revision": 3}
    assert prep._site_stp({}) == {"mode": "rstp", "region": "", "revision": 0}
    # VLAN správy lokality mimo řízení: síť, kde má adresu nejvíc zařízení; na VLAN rozhraní = na trunku s tagem
    obs = [{"vlan_id": None, "subnet": "192.168.93.0/24", "name": "bridge-vlan93"}, {"vlan_id": 90, "subnet": "192.168.90.0/24"},
           {"vlan_id": 93, "subnet": "192.168.93.0/24"}]
    sw = lambda ip: {"vlans": [{"name": "mng", "vlan-id": "93"}], "addresses": [{"address": ip, "interface": "mng"}]}
    gw = {"addresses": [{"address": "192.168.90.1/24", "interface": "v90"}, {"address": "192.168.93.1/24", "interface": "bridge-vlan93"}]}
    g = prep._guess_mgmt(obs, {"gw": gw, "s1": sw("192.168.93.40/24"), "s2": sw("192.168.93.41/24")})
    assert g["vid"] == 93 and g["native"] is False
    assert prep._guess_mgmt(obs, {"gw": gw}) is None


def test_lacp_bond():
    """LACP: členy nahradí v bridgi bond se společným nastavením; chyby v návrhu se odmítnou."""
    ports = [f"ether{i}" for i in range(1, 9)] + ["sfp-sfpplus1", "sfp-sfpplus2"]
    ctx = {"networks": [{"vid": v, "name": f"N{v}", "purpose": "lan", "subnet": None, "gateway": None, "dns": []} for v in (10, 20, 93)],
           "mgmt_vlan": 93, "mgmt_native": True, "managed": False, "models": [], "profiles": []}
    base = {"name": "T", "model": "X", "port_names": ports, "poe_ports": ports[:8], "unset": "off",
            "mgmt": {"vid": 93, "native": True, "mode": "dhcp"},
            "ports": {"ether1": {"mode": "hybrid", "vlan": 10, "tagged": [20], "edge": False, "comment": "NAS"},
                      "ether2": {"mode": "access", "vlan": 20, "poe": "off"},      # člen bondu převezme nastavení prvního, PoE si nechá
                      "ether5": {"mode": "access", "vlan": 10},
                      "sfp-sfpplus1": {"mode": "trunk"}, "sfp-sfpplus2": {"mode": "trunk"}}}
    plan = prep.validate({**base, "bonds": [{"ports": ["ether2", "ether1"]}, {"ports": ["sfp-sfpplus1", "sfp-sfpplus2"], "lacp_rate": "1sec"}]})
    assert plan["bonds"] == [{"name": "bond1", "ports": ["ether1", "ether2"], "lacp_rate": "30secs"},
                             {"name": "bond2", "ports": ["sfp-sfpplus1", "sfp-sfpplus2"], "lacp_rate": "1sec"}]
    assert plan["ports"]["ether2"] == {"mode": "hybrid", "vlan": 10, "tagged": [20], "edge": False, "comment": "NAS", "poe": "off"}
    m = prep.build(plan, ctx)
    assert [it["port"] for it in m["ports"] if it["mode"] != "off"] == ["bond1", "ether5", "bond2"] and not m["blockers"]
    assert m["carriers"] == ["bond2"] and any("LACP bond1" in w and "Hyper-V" in w for w in m["warnings"])
    s = prep.script(plan, ctx, "https://c.example/enroll/KEY", OPTS)
    assert _balanced(s) and s.isascii()
    i1 = s.index('/interface bonding add name="bond1" slaves="ether1,ether2" mode=802.3ad lacp-rate=30secs')
    i2 = s.index('/interface bridge port add bridge=bridge interface="bond1" pvid=10 frame-types=admit-all')
    assert i1 < i2 and 'slaves="sfp-sfpplus1,sfp-sfpplus2" mode=802.3ad lacp-rate=1sec' in s
    assert 'interface="ether1"' not in s and 'interface="ether2"' not in s            # členové sami v bridgi nejsou
    assert 'vlan-ids=20 tagged="bond1,bond2"' in s and 'vlan-ids=10 tagged="bond2" untagged="bond1,ether5"' in s
    assert '[find default-name="ether2"] poe-out=off' in s
    sm = {tuple(g["ports"]): g for g in prep.summary(plan, m)}
    assert sm[("ether1", "ether2")]["bond"] == "bond1" and sm[("ether5",)]["bond"] is None
    # porovnání po adopci: bond s jinými členy je rozdíl
    inv = {"bridge_ports": [{"interface": "bond1", "pvid": "10"}, {"interface": "ether5", "pvid": "10"}, {"interface": "bond2", "pvid": "93"}],
           "bridge_vlans": [{"vlan-ids": "20", "tagged": "bond1,bond2"}, {"vlan-ids": "10", "tagged": "bond2"}],
           "ethernet": [{"name": x, "default-name": x, "disabled": "true" if x in ("ether3", "ether4", "ether6", "ether7", "ether8") else "false"} for x in ports],
           "bonds": [{"name": "bond1", "slaves": "ether1,ether2"}, {"name": "bond2", "slaves": "sfp-sfpplus1"}]}
    d = prep.compare(plan, ctx, inv)["diffs"]
    assert [(x["port"], x["what"]) for x in d] == [("bond2", "bond")], d
    for bad in ([{"ports": ["ether1"]}], [{"ports": ["ether1", "ether9"]}], [{"ports": ["ether1", "ether2"]}, {"ports": ["ether2", "ether5"]}],
                [{"ports": ["ether3", "ether4"]}]):
        with pytest.raises(Exception):
            prep.validate({**base, "bonds": bad})


def test_ipv6():
    """IPv6 na připravovaném switchi: vypnuto (výchozí) / automaticky / ručně na rozhraní správy."""
    ports = [f"ether{i}" for i in range(1, 5)]
    ctx = {"networks": [{"vid": 93, "name": "MNG", "purpose": "mgmt", "subnet": None, "gateway": None, "dns": []}],
           "mgmt_vlan": 93, "mgmt_native": False, "managed": False, "models": [], "profiles": []}
    base = {"name": "T", "model": "X", "port_names": ports, "poe_ports": [], "unset": "off",
            "ports": {"ether4": {"mode": "trunk"}}, "mgmt": {"vid": 93, "native": False, "mode": "dhcp"}}
    gen = lambda v6: prep.script(prep.validate({**base, **({"ipv6": v6} if v6 is not None else {})}), ctx, "https://c.example/enroll/KEY", OPTS)
    s = gen(None)
    assert prep.validate(base)["ipv6"] == {"mode": "off"}
    assert ":do { /ipv6 settings set disable-ipv6=yes } on-error=" in s and "/ipv6 address" not in s and _balanced(s) and s.isascii()
    s = gen({"mode": "auto"})
    assert "disable-ipv6=no forward=no accept-router-advertisements=yes" in s and "/ipv6 address" not in s
    s = gen({"mode": "static", "address": "2001:DB8:93::20/64", "gateway": "2001:db8:93::1"})
    assert '/ipv6 address add address="2001:db8:93::20/64" interface="vlan93-mgmt" advertise=no comment="mtc"' in s
    assert '/ipv6 route add dst-address=::/0 gateway="2001:db8:93::1" comment="mtc mgmt"' in s and "accept-router-advertisements=no" in s
    assert _balanced(s) and s.isascii()
    s = gen({"mode": "static", "address": "2001:db8:93::20/64", "gateway": "fe80::1"})
    assert 'gateway="fe80::1%vlan93-mgmt"' in s                                   # link-local brána potřebuje rozhraní
    assert "/ipv6 route" not in gen({"mode": "static", "address": "2001:db8:93::20/64"})
    for bad in ({"mode": "static", "address": "2001:db8::20"}, {"mode": "static", "address": "10.0.0.1/24"},
                {"mode": "static", "address": "fe80::5/64"}, {"mode": "static", "address": "2001:db8::20/64", "gateway": "x"}):
        with pytest.raises(Exception):
            prep.validate({**base, "ipv6": bad})
