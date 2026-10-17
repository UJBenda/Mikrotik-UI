from fixtures import modern

from mtcontroller import backup, inventory
from mtcontroller.rosapi import PinMismatch, RosApiError


class FakeApi:
    """Napodobenina RosApi: odpovědi podle cesty, volitelné chyby."""

    def __init__(self, responses: dict, errors: dict | None = None):
        self.responses = responses
        self.errors = errors or {}
        self.calls = []
        self.timeout = 10

    def request(self, method, path, body=None, params=None):
        self.calls.append((method, path, body, params))
        if path in self.errors:
            raise self.errors[path]
        r = self.responses.get((method, path), self.responses.get(path))
        return r(body) if callable(r) else r

    def get(self, path, **params):
        return self.request("GET", path, params=params or None)

    def post(self, path, body):
        return self.request("POST", path, body)

    def delete(self, path):
        return self.request("DELETE", path)


def test_collect_skips_missing_menus_and_scrubs_secrets():
    responses = {s.path: [] for s in inventory.SECTIONS}
    responses["/system/resource"] = {"version": "7.20.1 (stable)", "board-name": "RB5009"}
    responses["/system/ntp/client"] = {"enabled": "true", "servers": "a", "auth-key-password": "x"}
    api = FakeApi(responses, {"/system/routerboard": RosApiError(400, "no such command")})
    data, errors = inventory.collect(api)
    assert "routerboard" not in data and errors == []
    assert "auth-key-password" not in data["ntp_client"]
    # proplist se posílá
    assert any(c[3] and ".proplist" in c[3] for c in api.calls)


def test_collect_aborts_on_pin_mismatch():
    import pytest

    api = FakeApi({}, {"/system/identity": PinMismatch(None, "x")})
    with pytest.raises(PinMismatch):
        inventory.collect(api)


def test_interface_samples_filter_dynamic():
    inv = modern()
    inv["interfaces"].append({"name": "<pppoe-x>", "type": "pppoe-in", "dynamic": "true"})
    names = [s["name"] for s in inventory.interface_samples(inv)]
    assert "<pppoe-x>" not in names and "ether1" in names
    s = next(x for x in inventory.interface_samples(inv) if x["name"] == "ether1")
    assert s["rx_bytes"] == 1000 and s["running"] is True


def test_basis_sha_changes_with_data():
    a = modern()
    b = modern()
    b["ntp_client"] = {"enabled": "true"}
    assert inventory.basis_sha(a, ["ntp_client"]) != inventory.basis_sha(b, ["ntp_client"])
    assert inventory.basis_sha(a, ["ntp_client"]) == inventory.basis_sha(modern(), ["ntp_client"])


EXPORT = "# 2026-10-17 20:00:00 by RouterOS 7.20.1\n# software id = X\n/system identity set name=gw\n" \
         "/user add name=x password=tajne123\n" + "/ip address add address=10.0.0.1/24 interface=ether2\n" * 1500


def test_fetch_export_reads_in_chunks():
    raw = EXPORT.encode()
    files = [{".id": "*1", "name": "mtc-backup.rsc", "size": str(len(raw))}]
    def read(body):
        off = int(body["offset"])
        return {"data": raw[off:off + int(body["chunk-size"])].decode()}
    api = FakeApi({("GET", "/file"): files, ("POST", "/export"): [], ("POST", "/file/read"): read,
                   ("DELETE", "/file/*1"): None})
    text = backup.fetch_export(api, wait_s=3)
    assert text == EXPORT
    reads = [c for c in api.calls if c[1] == "/file/read"]
    assert len(reads) == len(raw) // backup.CHUNK + 1
    assert ("DELETE", "/file/*1", None, None) in api.calls


def test_normalize_and_diff_redacts():
    a = "# 2026-10-17 20:00:00 by RouterOS 7.20.1\n/user add name=x password=tajne123\n"
    b = "# 2026-10-18 20:00:00 by RouterOS 7.20.1\n/user add name=x password=jine456\n"
    assert backup.normalize(a) != backup.normalize(b)
    d = backup.diff(a, b)
    assert "tajne123" not in d and "jine456" not in d and "password=***" in d
    assert "-/user add" in d and "+/user add" in d  # změna hesla je vidět
    assert backup.diff(a, a.replace("2026-10-17", "2026-10-19")) == ""
