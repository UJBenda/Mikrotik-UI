"""Wi-Fi lokality: rádia z nového wifi i starého wireless, klienti, CAPsMAN, varování."""

from mtcontroller import inventory, wifi

DEVS = [{"id": "a1", "serial": "S1", "identity": "ap-hala"}, {"id": "a2", "serial": "S2", "identity": "ap sklad"},
        {"id": "p1", "serial": "S3", "identity": "ptp-b"}]
INVS = {
    "a1": {
        "wifi": [
            {"name": "wifi1", "configuration.ssid": "Firma", "configuration.mode": "ap", "channel.band": "5ghz-ax",
             "channel.frequency": "5180", "channel.width": "20/40/80mhz", "running": "true", "disabled": "false"},
            {"name": "wifi2", "configuration.ssid": "Firma", "configuration.mode": "ap", "channel.band": "2ghz-ax",
             "channel.width": "20/40mhz", "channel.frequency": "2412", "running": "true", "disabled": "false"},
            {"name": "wifi3", "master-interface": "wifi1", "configuration.ssid": "Host", "running": "true",
             "disabled": "false"},
        ],
        "wifi_clients": [
            {"interface": "wifi1", "mac-address": "AA:00:00:00:00:01", "signal": "-55"},
            {"interface": "wifi1", "mac-address": "AA:00:00:00:00:02", "signal": "-81"},
            {"interface": "wifi3", "mac-address": "AA:00:00:00:00:03", "signal": "-60"},
        ],
        "capsman_caps": [{"name": "cap1", "identity": "cap-kancl", "address": "10.0.0.5", "board": "cAP ax",
                          "version": "7.20.1", "state": "Run"}],
    },
    "a2": {
        "wifi": [{"name": "wifi1", "configuration.ssid": "Firma", "configuration.mode": "ap",
                  "channel.frequency": "5180", "running": "false", "disabled": "false",
                  "security.passphrase": "TAJNE"}],
    },
    "p1": {
        "wireless": [{"name": "wlan1", "ssid": "ptp", "mode": "station-bridge", "band": "5ghz-a/n/ac",
                      "frequency": "5500", "channel-width": "20/40/80mhz-Ceee", "running": "true", "disabled": "false"}],
        "wireless_clients": [{"interface": "wlan1", "mac-address": "BB:00:00:00:00:01",
                              "signal-strength": "-63dBm@6Mbps"}],
    },
}


def test_site_wifi_radios_clients_and_warnings():
    w = wifi.site_wifi(DEVS, INVS)
    r = {(x["device"], x["interface"]): x for x in w["radios"]}
    assert r[("ap-hala", "wifi1")]["clients"] == 2
    assert r[("ap-hala", "wifi1")]["signal_avg"] == -68 and r[("ap-hala", "wifi1")]["weak_clients"] == 1
    assert r[("ap-hala", "wifi3")]["master"] == "wifi1" and r[("ap-hala", "wifi3")]["ssid"] == "Host"
    assert r[("ptp-b", "wlan1")]["driver"] == "wireless" and r[("ptp-b", "wlan1")]["signal_min"] == -63
    assert w["summary"] == {"radios": 5, "aps": 3, "clients": 4, "ssids": ["Firma", "Host", "ptp"]}
    assert w["caps"][0]["identity"] == "cap-kancl"
    texts = " | ".join(x["text"] for x in w["warnings"])
    assert "ap sklad wifi1: rádio neběží" in texts
    assert "slabým" not in texts and "1 klientů se signálem pod -75" in texts
    assert "40 MHz v pásmu 2,4 GHz" in texts
    # 5180 pevně na dvou AP (identita s mezerou je jedno zařízení), PtP stanice se nepočítá
    assert "kanál 5180 MHz" in texts and "5500" not in texts


def test_scrub_removes_wifi_secrets():
    scrubbed = inventory._scrub(INVS["a2"]["wifi"])
    assert "security.passphrase" not in scrubbed[0] and scrubbed[0]["configuration.ssid"] == "Firma"


def test_empty_site():
    w = wifi.site_wifi([], {})
    assert w["radios"] == [] and w["summary"]["clients"] == 0
