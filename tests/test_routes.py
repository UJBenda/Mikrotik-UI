"""Routy /32 na zařízení přes koncentrátor, ke kterému jsou připojená."""

import json

import pytest
from helpers import make_settings

from mtcontroller import routes


class _Prov:
    def __init__(self, name, sessions=None, fail=False):
        self.name, self.sessions, self.fail = name, sessions or {}, fail

    def active_sessions(self):
        if self.fail:
            raise ConnectionError("timeout")
        return self.sessions


class _Ip:
    """Podvržený příkaz ip: drží routy proto 186 a zaznamenává batch."""

    def __init__(self, routes_):
        self.routes, self.batches = dict(routes_), []

    def __call__(self, args, stdin=None):
        if args[:2] == ["-j", "-4"]:
            return json.dumps([{"dst": ip, "gateway": gw, "protocol": "186"} for ip, gw in self.routes.items()])
        assert args == ["-force", "-batch", "-"]
        self.batches.append(stdin)
        for ln in stdin.splitlines():
            p = ln.split()
            if p[1] == "replace":
                self.routes[p[2].split("/")[0]] = p[4]
            else:
                self.routes.pop(p[2].split("/")[0], None)
        return ""


def test_failover_moves_route_and_keeps_unknown(tmp_path):
    s = make_settings(tmp_path, concentrators=2)  # vpn1 10.10.0.1, vpn2 10.10.0.2
    ip = _Ip({"10.200.1.5": "10.10.0.1", "10.200.1.6": "10.10.0.1", "10.200.1.9": "10.10.0.2"})
    provs = [_Prov("vpn1", {"mtc-A": "10.200.1.6"}), _Prov("vpn2", {"mtc-B": "10.200.1.5", "mtc-C": "10.200.1.7"})]
    assert routes.sync(s, provs, run=ip) == 3
    # .5 přešlo na vpn2, .7 nová, .9 už nikde není → smazána, .6 beze změny
    assert ip.routes == {"10.200.1.5": "10.10.0.2", "10.200.1.6": "10.10.0.1", "10.200.1.7": "10.10.0.2"}
    assert routes.sync(s, provs, run=ip) == 0  # idempotentní
    # vpn2 neodpovídá: jeho routy zůstanou, nic se nemaže
    provs[1] = _Prov("vpn2", fail=True)
    assert routes.sync(s, provs, run=ip) == 0
    assert ip.routes["10.200.1.5"] == "10.10.0.2"


def test_addresses_outside_pool_or_garbage_are_ignored(tmp_path):
    s = make_settings(tmp_path)
    ip = _Ip({})
    provs = [_Prov("vpn1", {"a": "10.200.1.5", "b": "8.8.8.8", "c": "10.200.1.6; reboot", "d": "1.2.3.4/32"})]
    routes.sync(s, provs, run=ip)
    assert ip.routes == {"10.200.1.5": "10.10.0.1"}
    assert ";" not in "".join(ip.batches)


def test_route_via_overrides_api_host(tmp_path):
    s = make_settings(tmp_path)
    c = s.tunnel.concentrators[0].model_copy(update={"route_via": "192.168.5.1"})
    assert routes.gateway(c) == "192.168.5.1"
    with pytest.raises(Exception):
        type(c).model_validate({**c.model_dump(), "route_via": "vpn1.lan"})
