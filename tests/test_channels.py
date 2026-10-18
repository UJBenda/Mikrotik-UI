"""Plán kanálů Wi-Fi (obdoba Channel AI)."""

from mtcontroller import channels, wifi


def test_parse_channel():
    assert channels.parse_channel("5180/ax/Ceee") == (5180, 80)
    assert channels.parse_channel("2437/n/Ce") == (2437, 40)
    assert channels.parse_channel("2412/ax") == (2412, None)
    assert channels.parse_channel("5500/20mhz/ac") == (5500, 20)
    assert channels.parse_channel(None) == (None, None)
    assert channels.width_mhz("20/40/80mhz") == 80 and channels.width_mhz("20mhz") == 20
    assert channels.chan(5180) == 36 and channels.chan(2437) == 6


def R(dev, iface, freq, width=None, **kw):
    return {"device_id": dev, "device": dev, "interface": iface, "driver": "wifi", "disabled": False, "master": None,
            "mode": "ap", "band": None, "frequency": "auto", "width": None, "freq_now": freq, "width_now": width, **kw}


def test_plan_spreads_and_keeps_good_channels():
    radios = [R("ap1", "wifi1", 5180, 80), R("ap2", "wifi1", 5180, 80), R("ap3", "wifi1", 5260, 80),
              R("ap1", "wifi2", 2412, 20), R("ap2", "wifi2", 2412, 40), R("ap3", "wifi2", 2442, 20),
              R("ap4", "wifi1", 5180, 20, mode="station-bridge"),        # PtP klient – neplánuje se
              R("ap1", "wifi3", None, master="wifi1")]                   # virtuální AP
    p = channels.plan(radios)
    five = [i for i in p["items"] if i["band"] == "5"]
    assert [i["new"]["freq"] for i in five] == [5180, 5500, 5260]      # ap1 a ap3 zůstávají, ap2 jinam
    assert [i["change"] for i in five] == [False, True, False]
    assert five[1]["new"]["dfs"] and five[1]["reasons"] == ["rozložení AP na různé kanály"]
    two = [i for i in p["items"] if i["band"] == "2.4"]
    assert [i["new"]["channel"] for i in two] == [1, 6, 11]
    assert "v 2,4 GHz jen 20 MHz" in two[1]["reasons"] and "překrývá" in two[2]["reasons"][0]
    assert p["before"] == 3 and p["after"] == 0 and p["changes"] == 3
    assert five[1]["command"] == '/interface wifi set [find name="wifi1"] channel.frequency=5500 channel.width=20/40/80mhz'
    assert any("DFS" in n for n in p["notes"])


def test_width_by_density():
    radios = [R(f"ap{k}", "wifi1", 5180, 80) for k in range(6)]
    p = channels.plan(radios)
    assert {i["new"]["width"] for i in p["items"]} == {40}
    assert len({i["new"]["freq"] for i in p["items"]}) == 6
    assert p["after"] == 0
    assert channels.site_width(3) == 80 and channels.site_width(20) == 20


def test_site_wifi_uses_monitor():
    inv = {"wifi": [{"name": "wifi1", "configuration.ssid": "Firma", "configuration.mode": "ap",
                     "channel.band": "5ghz-ax", "running": "true"}],
           "wifi_monitor": [{"name": "wifi1", "channel": "5260/ax/Ceee"}]}
    w = wifi.site_wifi([{"id": "d1", "identity": "AP-1"}], {"d1": inv})
    r = w["radios"][0]
    assert r["freq_now"] == 5260 and r["width_now"] == 80 and r["frequency"] == "auto"
    item = w["channel_plan"]["items"][0]
    assert item["now"]["channel"] == 52 and item["now"]["auto"] and not item["change"]


def test_no_widening_and_off_plan_channels():
    p = channels.plan([R("ap1", "wifi1", 5180, 20), R("ap2", "wifi1", 5745, 80), R("ap3", "wifi1", 5600, 40)])
    a, b, c = p["items"]
    assert not a["change"] and a["new"]["width"] == 20                 # 20 MHz se nerozšiřuje
    assert b["change"] and "149" in b["reasons"][0] and b["new"]["freq"] in channels.CH5[80]
    assert c["change"] and "120–128" in c["reasons"][0] and c["new"]["width"] == 40


def test_busy_running_ap_keeps_channel():
    p = channels.plan([R("ap1", "wifi1", 5180, 80, running=False, clients=0),
                       R("ap2", "wifi1", 5180, 80, running=True, clients=12)])
    a, b = p["items"]
    assert a["change"] and not b["change"] and b["new"]["freq"] == 5180
