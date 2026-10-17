import pytest
from pydantic import ValidationError

from mtcontroller.config import load_settings


def test_defaults_without_file(tmp_path):
    s = load_settings(tmp_path / "missing.toml", environ={})
    assert s.tunnel.provider == "mikrotik"
    assert s.web.listen_host == "127.0.0.1"


def test_file_then_env_override(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[web]\nlisten_port = 9000\nui_host = "a.example"\n'
                 '[tunnel]\nconcentrator_hosts = ["vpn1"]\n')
    s = load_settings(p, environ={
        "MTC_WEB__UI_HOST": "b.example",
        "MTC_TUNNEL__CONCENTRATOR_HOSTS": '["vpn1", "vpn2"]',
    })
    assert s.web.listen_port == 9000
    assert s.web.ui_host == "b.example"
    assert s.tunnel.concentrator_hosts == ["vpn1", "vpn2"]


def test_public_pool_rejected(tmp_path):
    with pytest.raises(ValidationError):
        load_settings(tmp_path / "x.toml", environ={"MTC_TUNNEL__POOL": "8.8.0.0/16"})


def test_unknown_provider_rejected(tmp_path):
    with pytest.raises(ValidationError):
        load_settings(tmp_path / "x.toml", environ={"MTC_TUNNEL__PROVIDER": "openvpn"})
