import pytest
from pydantic import ValidationError

from mtcontroller.config import load_settings


def test_defaults_without_file(tmp_path):
    s = load_settings(tmp_path / "missing.toml", environ={})
    assert s.tunnel.provider == "mikrotik"
    assert s.web.listen_host == "127.0.0.1"


def test_example_config_is_valid():
    from pathlib import Path

    s = load_settings(Path(__file__).parent.parent / "config.toml.example", environ={})
    assert s.tunnel.concentrators[0].api_url == "https://10.10.0.1:8443"


def test_file_then_env_override(tmp_path):
    p = tmp_path / "config.toml"
    pw = tmp_path / "pw"
    pw.write_text("x\n")
    p.write_text('[web]\nlisten_port = 9000\nui_host = "a.example"\n'
                 '[[tunnel.concentrators]]\nname = "vpn1"\nconnect_host = "VPN1.example.cz"\n'
                 'api_url = "https://10.0.0.1:8443/"\n'
                 f'api_password_file = "{pw}"\napi_fingerprint = "{"AB:" * 31}AB"\n')
    s = load_settings(p, environ={"MTC_WEB__UI_HOST": "b.example"})
    assert s.web.listen_port == 9000 and s.web.ui_host == "b.example"
    c = s.tunnel.concentrators[0]
    assert c.connect_host == "vpn1.example.cz" and c.api_url == "https://10.0.0.1:8443"
    assert c.api_fingerprint == "ab" * 32 and c.password() == "x"


def test_invalid_values_rejected(tmp_path):
    bad = [
        {"MTC_TUNNEL__POOL": "8.8.0.0/16"},
        {"MTC_TUNNEL__PROVIDER": "openvpn"},
        {"MTC_WEB__UI_HOST": "bad host"},
        {"MTC_TUNNEL__CONTROLLER_IP": "10.200.0.5"},  # uvnitř poolu
        {"MTC_SECURITY__DEVICE_CHECK_CERTIFICATE": "maybe"},
    ]
    for env in bad:
        with pytest.raises(ValidationError):
            load_settings(tmp_path / "x.toml", environ=env)
