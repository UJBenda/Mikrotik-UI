import re

import pytest

from mtcontroller import routeros
from mtcontroller.routeros import FetchOpts, ProvisionParams, q, q_source


def test_quote_escapes_specials():
    assert q('a"b') == r'"a\"b"'
    assert q("a$b") == r'"a\$b"'
    assert q("a\\b") == r'"a\\b"'
    assert q("x?y") == r'"x\?y"'


def test_quote_rejects_non_ascii_and_newlines():
    for bad in ("ř", "a\nb", "a\rb", "\x00"):
        with pytest.raises(ValueError):
            q(bad)


def test_injection_attempt_stays_inside_string():
    evil = '"; /system reset-configuration; :put "'
    lit = q(evil)
    # uvnitř literálu nesmí být neescapovaná uvozovka
    inner = lit[1:-1]
    assert re.search(r'(?<!\\)"', inner) is None


def _balanced(script: str) -> bool:
    depth = 0
    in_str = False
    esc = False
    for ch in script:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not in_str


OPTS = FetchOpts("yes-without-crl")
P = ProvisionParams("10.200.0.1", "vpn1.example.cz", "mtc-HG409", "pw-sstp_123", "pw-api-456")


def test_all_scripts_are_balanced():
    scripts = [
        routeros.adopt_stage1("https://c.example/adopt/T/register", OPTS),
        routeros.adopt_stage2("https://c.example/api/checkin", "KEY_abc-123", OPTS),
        routeros.wrap_command("https://c.example/api/checkin/ack?id=5", "KEY", routeros.provision_body(P, OPTS), OPTS),
        routeros.wrap_command("https://c.example/api/checkin/ack?id=6", "KEY", "", OPTS, tail=routeros.REBOOT_TAIL),
        routeros.wrap_command("https://c.example/api/checkin/ack?id=7", "KEY", "", OPTS, tail=routeros.DEPROVISION_TAIL),
        routeros.error_script("token neplatny"),
        routeros.checkin_source("https://c.example/api/checkin", "KEY", OPTS),
    ]
    for s in scripts:
        assert _balanced(s), s
        s.encode("ascii")  # RouterOS skripty musí být čisté ASCII


def test_stage2_embeds_checkin_source_escaped():
    s = routeros.adopt_stage2("https://c.example/api/checkin", "KEY_abc", OPTS)
    m = re.search(r'source=(".*")\n', s)
    assert m, s
    lit = m.group(1)
    # zdroj je jeden řádek s \r\n místo skutečných konců řádků
    assert "\n" not in lit and "\\r\\n" in lit
    assert "Bearer KEY_abc" in lit
    assert _balanced(lit)


def test_provision_restricts_to_controller():
    body = routeros.provision_body(P, OPTS)
    assert 'verify-server-certificate=yes' in body
    assert 'address="10.200.0.1/32"' in body  # servisní účet jen ze správy přes koncentrátor
    assert "/ip route add" not in body  # žádná routa – check-in nesmí skončit v tunelu
    assert 'in-interface="mtc-sstp" action=drop' in body  # zbytek z tunelu zahodit
    assert 'chain=forward in-interface="mtc-sstp" action=drop' in body  # z tunelu nikam dál
    assert "add-default-route=no" in body
    # dosud vypnuté www-ssl se otevře jen pro správu, ne pro celý svět
    assert ':if ($wasOff) do={ /ip service set www-ssl address="10.200.0.1/32" }' in body
    assert body.index("wasOff [/ip service get www-ssl disabled]") < body.index("www-ssl certificate=")
    # žádné interaktivní přístupy pro servisní účet
    assert "ssh" not in routeros.MTC_GROUP_POLICY and "winbox" not in routeros.MTC_GROUP_POLICY


def test_https_certs_signed_by_local_ca(tmp_path):
    """RouterOS v7: tls-server certifikát bez CA nepodepíše („CA not found“) – podpis lokální CA."""
    from helpers import make_settings
    from mtcontroller.concentrator import concentrator_script

    st = make_settings(tmp_path)
    for s in (routeros.provision_body(P, OPTS), concentrator_script(st, st.tunnel.concentrators[0], "pw")):
        assert 'key-usage=key-cert-sign,crl-sign' in s and 'ca="mtc-ca"' in s
        assert s.index('/certificate sign "mtc-ca"') < s.index('ca="mtc-ca"')
        assert "private-key=yes" in s  # nedokončená šablona po chybě se nahradí
        assert _balanced(s)
    one = routeros.signed_cert("x", "cn")
    assert one.count("/certificate sign") == 2 and one.isascii()


def test_lab_mode_disables_verification_everywhere():
    lab = FetchOpts("no")
    assert "verify-server-certificate=no" in routeros.provision_body(P, lab)
    assert "check-certificate=no" in routeros.adopt_stage1("https://x/adopt/T/register", lab)


def test_reboot_after_ack():
    s = routeros.wrap_command("https://c/api/checkin/ack?id=1", "K", "", OPTS, tail=routeros.REBOOT_TAIL)
    assert s.index("/tool fetch") < s.index("/system reboot")


def test_q_source_rejects_non_ascii():
    with pytest.raises(ValueError):
        q_source("ř")


def test_concentrator_script_is_ascii_and_balanced(tmp_path):
    from helpers import make_settings
    from mtcontroller.concentrator import concentrator_script

    st = make_settings(tmp_path)
    s = concentrator_script(st, st.tunnel.concentrators[0], "heslo_123")
    assert _balanced(s)
    s.encode("ascii")
    assert 'local-address="10.200.0.1"' in s and "port=8443" in s
    assert 'address=($ctrl . "/32")' in s
    assert 'key-type' in s and '[Rr][Ss][Aa]' in s  # SSTP jen s RSA certifikátem
    assert 'action=src-nat to-addresses="10.200.0.1"' in s


def test_scheduler_has_explicit_start_and_quiet_fetch():
    # RouterOS 7.24–7.24.4 nespouští scheduler s výchozím startem
    s = routeros.adopt_stage2("https://c.example/api/checkin", "K", OPTS)
    assert "start-date=[/system clock get date] start-time=00:00:00" in s
    src = routeros.checkin_source("https://c.example/api/checkin", "K", OPTS)
    assert "as-value]" in src  # výstup fetch se nehromadí ve skriptu


def test_firewall_move_skips_dynamic_rules(tmp_path):
    # výchozí konfigurace s fasttrackem má na začátku dynamické pravidlo,
    # před které RouterOS přesun odmítne → provisioning by selhal
    from helpers import make_settings
    from mtcontroller.concentrator import concentrator_script

    st = make_settings(tmp_path)
    for s in (routeros.provision_body(P, OPTS),
              concentrator_script(st, st.tunnel.concentrators[0], "heslo_123")):
        moves = [ln for ln in s.splitlines() if " move " in ln]
        assert moves
        for ln in moves:
            assert "find dynamic=no]->0" in ln, ln
        assert re.search(r"find\]\s*->", s) is None


def test_upgrade_scripts():
    for s in (routeros.wrap_command("https://c/api/checkin/ack?id=1", "K", routeros.upgrade_body("7.20.5"), OPTS,
                                    tail=routeros.upgrade_tail("7.20.5")),
              routeros.wrap_command("https://c/api/checkin/ack?id=2", "K", routeros.FIRMWARE_BODY, OPTS,
                                    tail=routeros.FIRMWARE_TAIL)):
        assert _balanced(s), s
        s.encode("ascii")
    for bad in ("7.20.5; /system reset-configuration", "latest", "", '7.1"'):
        with pytest.raises(ValueError):
            routeros.upgrade_body(bad)
        with pytest.raises(ValueError):
            routeros.upgrade_tail(bad)


def test_takeover_scripts():
    """Převzetí do řízení (switchcfg): příkaz, bootstrap, potvrzení, sladění – ASCII a vyvážené."""
    import test_switchcfg as t

    from mtcontroller import switchcfg

    m = switchcfg.build(t.INV, t.INTENT, {"ports": {"sfp1": {"mode": "off"}}}, model_name="CRS326", identity="SW")
    boot = switchcfg.bootstrap(m, routeros.provision_body(P, OPTS),
                               routeros.checkin_install("https://c.example/api/checkin", "KEY", OPTS),
                               reverted_url="https://c.example/api/checkin/ack?id=9&status=reverted",
                               auth_header="Authorization: Bearer KEY", check_certificate="yes-without-crl",
                               backup_password="PW")
    cmd = routeros.wrap_command("https://c.example/api/checkin/ack?id=9", "KEY",
                                switchcfg.takeover_body("https://c.example/api/checkin/bootstrap?id=9",
                                                        "Authorization: Bearer KEY", "yes-without-crl", "PW"),
                                OPTS, tail=switchcfg.TAKEOVER_TAIL)
    conf = routeros.wrap_command("https://c.example/api/checkin/ack?id=10", "KEY", switchcfg.TAKEOVER_CONFIRM, OPTS)
    rec = switchcfg.reconcile(t._managed_inv(), switchcfg.build(t._managed_inv(), t.INTENT, {"ports": {"ether2": {"mode": "off"}}}))
    for s in (boot, cmd, conf, rec["apply"], rec["revert"]):
        assert _balanced(s), s
        s.encode("ascii")
    # check-in hlásí běžící převzetí
    assert 'comment="mtc-takeover"' in routeros.checkin_source("https://c.example/api/checkin", "KEY", OPTS)


def test_default_config_scripts():
    """Výchozí konfigurace podle role a zónový firewall (defaults, fwmatrix) – ASCII a vyvážené."""
    from mtcontroller import defaults

    for role in defaults.ROLES:
        s = defaults.script(role, "https://c.example/enroll/KEY?role=" + role, OPTS)
        assert _balanced(s), role
        s.encode("ascii")
    s = defaults.one_liner("https://c.example/enroll/KEY/default/switch", OPTS)
    assert _balanced(s)
    s.encode("ascii")


def test_wifi_vlan_scripts():
    """Skript mtc-wifi-vlan na AP (VLAN rádií podle MAC) – ASCII, vyvážený, i s prázdnou tabulkou."""
    from mtcontroller import wificfg

    for table in ([{"mac": "0A:55:31:53:B9:2E", "ssid": "Firma", "pvid": 10, "tagged": [30, 40]},
                   {"mac": "08:55:31:53:B9:2F", "ssid": "Hoste", "pvid": 20, "tagged": []}], []):
        s = routeros.wrap_command("https://c.example/api/checkin/ack?id=11", "KEY", wificfg.cap_vlan_body(table), OPTS)
        assert _balanced(s), s
        s.encode("ascii")
        assert _balanced(wificfg.cap_vlan_source(table))


def test_identity_scripts():
    """Přejmenování zařízení a jména CAPsMANu na AP – ASCII, vyvážené, bez vložení příkazu."""
    import pytest

    from mtcontroller import services

    for body in (services.identity_body("SW-sklad_1.patro"), services.cap_names_body(["SW-core", "SW-hlavni"])):
        s = routeros.wrap_command("https://c.example/api/checkin/ack?id=12", "KEY", body, OPTS)
        assert _balanced(s), s
        s.encode("ascii")
    for bad in ('x"; /system reset', "Sklad přízemí", "a b", "", "-x", "x" * 64):
        with pytest.raises(services.ServiceError):
            services.identity_body(bad)
