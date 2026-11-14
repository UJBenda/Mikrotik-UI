"""Optimalizace kanálů a výkonu Wi-Fi podle měření okolí (rfopt)."""

from mtcontroller import rfopt, wificfg


def _radio(dev, iface, band, freq, width=20, mac=None, signals=()):
    return {"device_id": dev, "device": dev.upper(), "interface": iface, "mac": mac or f"AA:BB:CC:00:{dev[-2:]}:0{iface[-1]}",
            "std": "5ghz-ax" if band == "5ghz" else "2ghz-ax", "band": band, "freq_now": freq, "width_now": width,
            "clients": 0, "signals": list(signals)}


def _obs(*bss):
    return {"at": "2026-11-14T10:00:00+00:00",
            "bss": [{"bssid": b[0], "frequency": b[1], "signal": b[2], "kind": b[3] if len(b) > 3 else "neighbor",
                     "ssid": b[4] if len(b) > 4 else "soused"} for b in bss]}


def test_moves_away_from_neighbors_and_keeps_good_channel():
    radios = [_radio("ap01", "wifi2", "2ghz", 2412), _radio("ap02", "wifi2", "2ghz", 2462)]
    obs = {
        # ap01 slyší na kanálu 1 dva silné sousedy, kanál 6 je volný; ap02 má na 11 klid
        "ap01": _obs(("11:11:11:11:11:01", 2412, -48), ("11:11:11:11:11:02", 2417, -55), ("11:11:11:11:11:03", 2462, -60)),
        "ap02": _obs(("11:11:11:11:11:04", 2412, -80)),
    }
    p = rfopt.plan(radios, obs, lower_tx=False)
    by = {i["device_id"]: i for i in p["items"]}
    assert by["ap01"]["new"]["channel"] == 6 and by["ap01"]["change"] is True
    assert by["ap02"]["new"]["channel"] == 11 and by["ap02"]["change"] is False
    assert p["after"] < p["before"] and p["changes"] == 1 and p["missing"] == []
    assert by["ap01"]["foreign"] == 3 and by["ap01"]["strongest"]["signal"] == -48
    assert by["ap01"]["now"]["cost"] > by["ap01"]["new"]["cost"]


def test_own_aps_that_hear_each_other_split_and_tx_drops():
    # dvě AP na stejném 80 MHz bloku se slyší −55 dBm; třetí je daleko (neslyší nikoho)
    radios = [_radio("ap01", "wifi1", "5ghz", 5180, 80), _radio("ap02", "wifi1", "5ghz", 5180, 80),
              _radio("ap03", "wifi1", "5ghz", 5180, 80)]
    obs = {"ap01": _obs(("AA:BB:CC:00:02:01", 5180, -55, "own")), "ap02": _obs(("AA:BB:CC:00:01:01", 5180, -57, "own")),
           "ap03": _obs()}
    p = rfopt.plan(radios, obs, lower_tx=True)
    by = {i["device_id"]: i for i in p["items"]}
    assert by["ap01"]["new"]["freq"] != by["ap02"]["new"]["freq"], p["items"]
    assert p["after"] == 0 and p["before"] > 0
    # ap03 nikoho neslyší → může zůstat, kde je, a výkon se nesnižuje
    assert by["ap03"]["change"] is False
    assert all("slyší" not in i["tx_reason"] for i in p["items"])
    # bez DFS a jen 80 MHz je jediný blok: zúží se na 40 MHz, aby se AP rozešla
    p = rfopt.plan(radios[:2], {k: obs[k] for k in ("ap01", "ap02")}, dfs=False, lower_tx=True)
    assert sorted((i["new"]["freq"], i["new"]["width"]) for i in p["items"]) == [(5180, 40), (5220, 40)], p["items"]
    assert not any(i["new"]["dfs"] for i in p["items"])
    # tři AP v 2,4 GHz, čtvrté musí sdílet kanál s tím, které slyší nejslabší → sníží se mu výkon
    r24 = [_radio(f"ap0{n}", "wifi2", "2ghz", f) for n, f in ((1, 2412), (2, 2437), (3, 2462), (4, 2412))]
    m = lambda n: f"AA:BB:CC:00:0{n}:02"  # noqa: E731
    f = {1: 2412, 2: 2437, 3: 2462, 4: 2412}
    o24 = {"ap01": _obs((m(2), f[2], -50, "own"), (m(3), f[3], -50, "own"), (m(4), f[4], -60, "own")),
           "ap02": _obs((m(1), f[1], -50, "own"), (m(3), f[3], -50, "own"), (m(4), f[4], -50, "own")),
           "ap03": _obs((m(1), f[1], -50, "own"), (m(2), f[2], -50, "own"), (m(4), f[4], -52, "own")),
           "ap04": _obs((m(1), f[1], -58, "own"), (m(2), f[2], -50, "own"), (m(3), f[3], -52, "own"))}
    p = rfopt.plan(r24, o24, lower_tx=True)
    a4 = next(i for i in p["items"] if i["device_id"] == "ap04")
    assert a4["new"]["channel"] == 1 and "AP01" in a4["tx_reason"] and "−4 dB" in a4["tx_reason"], a4
    full = rfopt.plan(r24, o24, lower_tx=False)
    assert next(i for i in full["items"] if i["device_id"] == "ap04")["tx_power"] == a4["tx_power"] + 4
    # klienti daleko → výkon se nesnižuje
    r24[3]["signals"] = [-75, -78, -80]
    a4 = next(i for i in rfopt.plan(r24, o24, lower_tx=True)["items"] if i["device_id"] == "ap04")
    assert "nesnížen" in a4["tx_reason"]


def test_missing_scan_is_reported_and_pins_validated():
    radios = [_radio("ap01", "wifi1", "5ghz", 5180, 80), _radio("ap02", "wifi1", "5ghz", 5180, 80)]
    p = rfopt.plan(radios, {"ap01": _obs()})
    assert p["missing"] == ["AP02"] and any("Bez měření" in n for n in p["notes"])
    # bez měření dvojice počítá s odhadem → přesto se rozejdou
    assert len({i["new"]["freq"] for i in p["items"]}) == 2
    rf = {"radios": {"ap01/wifi1": {"freq": 5500, "width": 80, "tx": 14}, "ap02/wifi1": {"freq": 2412, "width": 20},
                     "ap03/wifi1": "x"}}
    assert rfopt.pin_for(rf, "ap01", "wifi1", "5", {"dfs": True}) == {"freq": 5500, "width": 80, "tx": 14}
    assert rfopt.pin_for(rf, "ap01", "wifi1", "5", {"dfs": False}) is None          # DFS mezitím zakázané
    assert rfopt.pin_for(rf, "ap01", "wifi1", "5", {"max_width": 40})["width"] == 40
    assert rfopt.pin_for(rf, "ap02", "wifi1", "5", {}) is None                        # jiné pásmo
    assert rfopt.pin_for(rf, "ap03", "wifi1", "5", {}) is None and rfopt.pin_for(None, "a", "b", "5", {}) is None


def test_capsman_uses_pins():
    radios = [dict(_radio("ap01", "wifi1", "5ghz", 5180, 80), group=None, chip="ax", frequency="auto", driver="wifi",
                   running=True, disabled=False)]
    ssids = [{"id": "s1", "ssid": "Firma", "vlan_id": 10, "bands": "2.4,5", "isolation": False, "ft": True, "enabled": True,
              "groups": [], "ppsk": []}]
    plain = wificfg.manager(ssids, radios, {}, host_has_radios=False)
    assert "frequency=5180" in plain["script"] and plain["radios"][0]["optimized"] is False
    m = wificfg.manager(ssids, radios, {}, host_has_radios=False,
                        rf={"radios": {"ap01/wifi1": {"freq": 5500, "width": 40, "tx": 12}}})
    assert "width=20/40mhz frequency=5500" in m["script"] and "tx-power=12" in m["script"], m["script"]
    r = m["radios"][0]
    assert r["channel"] == 100 and r["width"] == 40 and r["tx_power"] == 12 and r["optimized"] is True
    assert m["channel_plan"]["items"][0]["new"]["channel"] == 100
