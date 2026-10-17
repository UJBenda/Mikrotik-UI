"""Administrační příkazy: `python3 -m mtcontroller.cli <příkaz>` (alias `mtc-admin`).

  migrate                       aplikuje migrace (spouští root, role mtc_owner)
  gen-master-key                vypíše nový master klíč (base64)
  create-superadmin EMAIL       založí superadmina (heslo interaktivně)
  create-user EMAIL ROLE TENANT založí uživatele s rolí v tenantovi
  create-tenant NAME            založí tenanta (přidělí tunelový blok)
  create-site TENANT NAME       založí lokalitu
  token TENANT [SITE]           vygeneruje adopční příkaz pro MikroTik
  devices TENANT                vypíše zařízení
  approve TENANT DEVICE SITE    schválí zařízení
  concentrator-script NAME      vypíše .rsc pro nastavení koncentrátoru
  concentrator-check            ověří REST spojení na všechny koncentrátory
  notify-test NAME              pošle zkušební notifikaci na cíl z [[notify]]
  doctor                        zkontroluje instalaci (konfigurace, DB, síť, koncentrátory)
  device-check TENANT DEVICE    diagnostika REST přes tunel (identita, otisk, porty, aktualizace)
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys

from .config import get_settings


def _db():
    from .db import get_db
    return get_db()


def _password() -> str:
    pw = getpass.getpass("Heslo (min. 12 znaků): ")
    if pw != getpass.getpass("Znovu: "):
        raise SystemExit("Hesla se neshodují")
    return pw


def _tenant_id(ref: str) -> str:
    from .db import operator_scope
    with operator_scope(_db()) as tx:
        row = tx.fetchone("SELECT id FROM tenant WHERE id::text = %(r)s OR name = %(r)s", {"r": ref})
    if not row:
        raise SystemExit(f"tenant {ref!r} neexistuje")
    return str(row["id"])


def _site_id(tenant_id: str, ref: str) -> str:
    from .db import tenant_scope
    with tenant_scope(_db(), tenant_id) as tx:
        row = tx.fetchone("SELECT id FROM site WHERE id::text = %(r)s OR name = %(r)s", {"r": ref})
    if not row:
        raise SystemExit(f"lokalita {ref!r} neexistuje")
    return str(row["id"])


def cmd_migrate(a) -> int:
    from . import migrate
    from .db import PsycopgDatabase
    s = get_settings()
    ran = migrate.upgrade(PsycopgDatabase(s.database.migrate_dsn), s.database.app_role)
    print("aplikováno:", ran or "nic, schéma je aktuální")
    return 0


def cmd_gen_master_key(a) -> int:
    from .security import SecretBox
    print(SecretBox.generate_master_key())
    return 0


def cmd_create_superadmin(a) -> int:
    from . import auth
    from .db import operator_scope
    from .services import audit
    pw = _password()
    with operator_scope(_db()) as tx:
        uid = auth.create_user(tx, a.email, pw)
        tx.execute("INSERT INTO role_binding (user_id, role) VALUES (%(u)s, 'superadmin')", {"u": uid})
        audit(tx, "user.create_superadmin", target=f"user:{uid}", after={"email": a.email.lower()})
    print(f"Superadmin {a.email} založen. MFA se nastaví při prvním přihlášení v UI.")
    return 0


def cmd_create_user(a) -> int:
    from . import auth
    from .db import operator_scope
    from .services import audit
    if a.role not in ("tenant_admin", "technician", "readonly"):
        raise SystemExit("role: tenant_admin | technician | readonly")
    tid = _tenant_id(a.tenant)
    pw = _password()
    with operator_scope(_db()) as tx:
        uid = auth.create_user(tx, a.email, pw)
        tx.execute("INSERT INTO role_binding (user_id, role, tenant_id) VALUES (%(u)s, %(r)s, %(t)s)",
                   {"u": uid, "r": a.role, "t": tid})
        audit(tx, "user.create", tenant_id=tid, target=f"user:{uid}", after={"email": a.email.lower(), "role": a.role})
    print(f"Uživatel {a.email} ({a.role}) založen.")
    return 0


def cmd_create_tenant(a) -> int:
    from .services import create_tenant
    row = create_tenant(_db(), get_settings(), a.name)
    print(f"Tenant {row['name']} založen: id={row['id']} blok={row['tunnel_block']}")
    return 0


def cmd_create_site(a) -> int:
    from .services import create_site
    row = create_site(_db(), _tenant_id(a.tenant), a.name)
    print(f"Lokalita {row['name']} založena: id={row['id']}")
    return 0


def cmd_token(a) -> int:
    from .db import operator_scope
    from .services import create_adoption_token
    tid = _tenant_id(a.tenant)
    sid = _site_id(tid, a.site) if a.site else None
    with operator_scope(_db()) as tx:
        u = tx.fetchone("""SELECT u.id FROM app_user u JOIN role_binding r ON r.user_id = u.id
                           WHERE r.role = 'superadmin' ORDER BY u.created_at LIMIT 1""")
    if not u:
        raise SystemExit("nejdřív založ superadmina (create-superadmin)")
    out = create_adoption_token(_db(), get_settings(), tid, sid, u["id"])
    print(f"Platí do {out.expires_at}, jednorázově. Vlož do terminálu MikroTiku:\n")
    print(out.command)
    return 0


def cmd_devices(a) -> int:
    from .db import tenant_scope
    tid = _tenant_id(a.tenant)
    with tenant_scope(_db(), tid) as tx:
        rows = tx.fetchall("""SELECT d.id, d.serial, d.model, d.ros_version, d.state, host(d.tunnel_ip) AS ip,
                                     s.name AS site, d.last_checkin_at, d.last_status->>'sstp' AS sstp
                              FROM device d LEFT JOIN site s ON s.id = d.site_id ORDER BY d.created_at""")
    for r in rows:
        print(f"{r['id']}  {r['state']:<12} {r['serial']:<16} {r['model'] or '':<20} {r['ros_version'] or '':<18} "
              f"{r['site'] or '-':<12} {r['ip'] or '-':<14} sstp={r['sstp'] or '-'}  checkin={r['last_checkin_at']}")
    return 0


def device_check(db, box, tid: str, ref: str, out=print) -> int:
    """Diagnostika REST přes tunel: identita, otisk certifikátu, monitor portů, aktualizace."""
    from . import inventory
    from .db import tenant_scope
    from .rosapi import PinMismatch, RosApiError
    from .services import device_api

    with tenant_scope(db, tid) as tx:
        dev = tx.fetchone("""SELECT id, serial, state, host(tunnel_ip) AS tunnel_ip, api_cert_fingerprint AS fp
                             FROM device WHERE id::text = %(r)s OR serial = %(r)s""", {"r": ref})
        if not dev:
            raise SystemExit(f"zařízení {ref!r} neexistuje")
        out(f"{dev['serial']}: stav {dev['state']}, tunel {dev['tunnel_ip'] or '-'}, otisk {dev['fp'] or '-'}")
        try:
            api = device_api(tx, box, dev, timeout=15)
        except Exception as e:  # noqa: BLE001
            out(f"  [XX] {e}")
            return 1
    ok = True
    checks = [
        ("identita", lambda: api.get("/system/identity").get("name")),
        ("verze", lambda: api.get("/system/resource").get("version")),
        ("ethernet monitor", lambda: ", ".join(
            f"{m.get('name')}={m.get('rate') or m.get('status')}" for m in (api.post(
                "/interface/ethernet/monitor",
                {"numbers": ",".join(e["name"] for e in api.get("/interface/ethernet", **{".proplist": "name"})[:4]),
                 "once": "true", ".proplist": inventory.MONITOR_PROPS}) or []))),
        ("aktualizace", lambda: str(api.get("/system/package/update"))),
    ]
    try:
        for name, fn in checks:
            try:
                out(f"  [OK] {name}: {fn()}")
            except PinMismatch:
                raise
            except RosApiError as e:
                ok = False
                out(f"  [XX] {name}: {e}")
    except PinMismatch as e:
        out(f"  [XX] otisk certifikátu: {e}")
        return 1
    finally:
        api.close()
    return 0 if ok else 1


def cmd_device_check(a) -> int:
    from .security import get_box
    return device_check(_db(), get_box(), _tenant_id(a.tenant), a.device)


def cmd_approve(a) -> int:
    from .security import get_box
    from .services import approve_device
    from .tunnel import build_providers
    s = get_settings()
    tid = _tenant_id(a.tenant)
    errors = approve_device(_db(), s, get_box(), build_providers(s), tid, a.device,
                            _site_id(tid, a.site), a.role)
    print("Schváleno. Provisioning proběhne při dalším check-inu (do 1 min).")
    for e in errors:
        print("VAROVÁNÍ koncentrátor:", e, file=sys.stderr)
    return 0


def cmd_concentrator_script(a) -> int:
    from .concentrator import concentrator_script
    s = get_settings()
    c = next((c for c in s.tunnel.concentrators if c.name == a.name), None)
    if not c:
        raise SystemExit(f"koncentrátor {a.name!r} není v konfiguraci")
    print(concentrator_script(s, c, c.password()))
    return 0


def cmd_concentrator_check(a) -> int:
    from .tunnel import build_providers
    ok = True
    for p in build_providers(get_settings()):
        try:
            sessions = p.active_sessions()
            ident = p.api.get("/system/identity")
            print(f"{p.name}: OK ({ident.get('name')}), aktivních SSTP relací: {len(sessions)}")
        except Exception as e:
            ok = False
            print(f"{p.name}: CHYBA {e}", file=sys.stderr)
    return 0 if ok else 1


def cmd_notify_test(a) -> int:
    from .notify import test_message
    test_message(get_settings(), a.name)
    print("Zkušební notifikace odeslána.")
    return 0


def cmd_doctor(a) -> int:
    from . import doctor
    from .db import PsycopgDatabase
    s = get_settings()
    checks = doctor.run(s, _db(), PsycopgDatabase(s.database.migrate_dsn))
    print(doctor.render(checks))
    bad = [c for c in checks if c.status == doctor.FAIL]
    print("\nVšechno v pořádku." if not bad else f"\nProblémů: {len(bad)}")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mtc-admin", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate").set_defaults(fn=cmd_migrate)
    sub.add_parser("gen-master-key").set_defaults(fn=cmd_gen_master_key)
    x = sub.add_parser("create-superadmin"); x.add_argument("email"); x.set_defaults(fn=cmd_create_superadmin)
    x = sub.add_parser("create-user"); x.add_argument("email"); x.add_argument("role"); x.add_argument("tenant")
    x.set_defaults(fn=cmd_create_user)
    x = sub.add_parser("create-tenant"); x.add_argument("name"); x.set_defaults(fn=cmd_create_tenant)
    x = sub.add_parser("create-site"); x.add_argument("tenant"); x.add_argument("name"); x.set_defaults(fn=cmd_create_site)
    x = sub.add_parser("token"); x.add_argument("tenant"); x.add_argument("site", nargs="?"); x.set_defaults(fn=cmd_token)
    x = sub.add_parser("devices"); x.add_argument("tenant"); x.set_defaults(fn=cmd_devices)
    x = sub.add_parser("approve"); x.add_argument("tenant"); x.add_argument("device"); x.add_argument("site")
    x.add_argument("--role", default="unknown"); x.set_defaults(fn=cmd_approve)
    x = sub.add_parser("concentrator-script"); x.add_argument("name"); x.set_defaults(fn=cmd_concentrator_script)
    sub.add_parser("concentrator-check").set_defaults(fn=cmd_concentrator_check)
    x = sub.add_parser("notify-test"); x.add_argument("name"); x.set_defaults(fn=cmd_notify_test)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    x = sub.add_parser("device-check"); x.add_argument("tenant"); x.add_argument("device")
    x.set_defaults(fn=cmd_device_check)
    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except Exception as e:  # čitelná chyba místo tracebacku
        from .services import ServiceError
        if isinstance(e, ServiceError):
            raise SystemExit(f"Chyba: {e}")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
