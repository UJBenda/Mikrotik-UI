"""Plány změn konfigurace (požadovaný stav → diff → schválení → aplikace).

Zásady:
- Modul smí na zařízení zapisovat jen v režimu `manage` (device_module).
  Výchozí je `observe` – controller jen čte a ukazuje odchylky.
- Plán vzniká z konkrétní inventury (basis_sha). Pokud se zařízení mezitím
  změní, plán se při schválení zneplatní (superseded) a je nutné ho vygenerovat znovu.
- U modulů v security.four_eyes_modules musí plán schválit jiný uživatel než autor.
- Aplikace jde přes frontu příkazů (kind 'apply_plan'); výsledek se potvrdí ackem.
"""

from __future__ import annotations

from typing import Any

from . import dhcpguard, inventory, leases, ntp, portcfg
from .config import Settings
from .db import Database, Tx, tenant_scope
from .services import NotFound, ServiceError, _s, audit, enqueue

MODULES = {"ntp", "ports", "vlan", "firewall", "capsman", "dhcp_snooping", "dhcp"}
MANAGEABLE = {"ntp", "ports", "dhcp_snooping", "dhcp"}
CONFIRM_MINUTES = 5  # výchozí okno pro potvrzení změny s automatickým návratem


def basis_for(module: str, inv: dict) -> str:
    if module == "takeover":
        from .takeover import BASIS_SECTIONS

        return inventory.basis_sha(inv, BASIS_SECTIONS)
    if module == "switch":
        from .switchcfg import basis

        return basis(inv)
    if module == "capsman":
        from .wificfg import basis

        return basis(inv)
    if module == "hardening":
        from .hardening import FIX_SECTIONS

        return inventory.basis_sha(inv, FIX_SECTIONS)
    if module == "wifitable":
        from .wificfg import table_basis

        return table_basis(inv)
    if module == "ports":
        return portcfg.config_basis(inv)
    if module == "dhcp_snooping":
        return dhcpguard.basis(inv)
    if module == "dhcp":
        return leases.config_basis(inv)
    return inventory.basis_sha(inv, ntp.SECTIONS if module == "ntp" else [])


def set_module_mode(db: Database, tenant_id: Any, device_id: Any, module: str, mode: str,
                    user_id: Any = None, ip: str | None = None) -> None:
    if module not in MODULES or mode not in ("observe", "manage"):
        raise ServiceError("neplatný modul nebo režim")
    if module not in MANAGEABLE and mode == "manage":
        raise ServiceError(f"modul {module} zatím umí jen režim observe")
    with tenant_scope(db, tenant_id) as tx:
        if not tx.fetchone("SELECT 1 FROM device WHERE id = %(d)s AND state <> 'removed'", {"d": str(device_id)}):
            raise NotFound("zařízení neexistuje")
        tx.execute(
            """INSERT INTO device_module (tenant_id, device_id, module, mode, changed_by)
               VALUES (%(t)s, %(d)s, %(m)s, %(mode)s, %(u)s)
               ON CONFLICT (device_id, module) DO UPDATE
                 SET mode = EXCLUDED.mode, changed_by = EXCLUDED.changed_by, changed_at = now()""",
            {"t": str(tenant_id), "d": str(device_id), "m": module, "mode": mode, "u": _s(user_id)})
        audit(tx, "device.module_mode", tenant_id=tenant_id, user_id=user_id, target=f"device:{device_id}",
              after={"module": module, "mode": mode}, ip=ip)


def module_modes(tx: Tx, device_id: Any) -> dict[str, str]:
    rows = tx.fetchall("SELECT module, mode FROM device_module WHERE device_id = %(d)s", {"d": str(device_id)})
    return {m: "observe" for m in MODULES} | {r["module"]: r["mode"] for r in rows}


def set_site_policy(db: Database, tenant_id: Any, site_id: Any, module: str, data: dict,
                    user_id: Any = None, ip: str | None = None) -> dict:
    if module not in ("ntp", "dhcp_snooping"):
        raise ServiceError("politika lokality je jen pro NTP a DHCP snooping")
    with tenant_scope(db, tenant_id) as tx:
        if module == "ntp":
            try:
                policy = ntp.validate_policy(data)
            except ntp.PolicyError as e:
                raise ServiceError(str(e)) from e
            dev = tx.fetchone("SELECT site_id, state FROM device WHERE id = %(d)s",
                              {"d": policy["primary_device_id"]})
            if not dev or str(dev["site_id"]) != str(site_id):
                raise ServiceError("primární zařízení musí patřit do této lokality")
        else:
            ids = {str(r["id"]) for r in tx.fetchall(
                "SELECT id FROM device WHERE site_id = %(s)s AND state IN ('adopted', 'offline')", {"s": str(site_id)})}
            try:
                policy = dhcpguard.validate_policy(data, ids)
            except ValueError as e:
                raise ServiceError(str(e)) from e
        tx.execute(
            """INSERT INTO site_policy (tenant_id, site_id, module, data, updated_by)
               VALUES (%(t)s, %(s)s, %(m)s, %(d)s, %(u)s)
               ON CONFLICT (site_id, module) DO UPDATE
                 SET data = EXCLUDED.data, updated_by = EXCLUDED.updated_by, updated_at = now()""",
            {"t": str(tenant_id), "s": str(site_id), "m": module, "d": policy, "u": _s(user_id)})
        audit(tx, "site.policy", tenant_id=tenant_id, user_id=user_id, target=f"site:{site_id}",
              after={"module": module, **policy}, ip=ip)
    return policy


def get_site_policy(tx: Tx, site_id: Any, module: str) -> dict | None:
    r = tx.fetchone("SELECT data FROM site_policy WHERE site_id = %(s)s AND module = %(m)s",
                    {"s": str(site_id), "m": module})
    return r["data"] if r else None


def generate_ntp_plans(db: Database, tenant_id: Any, site_id: Any, user_id: Any = None,
                       ip: str | None = None) -> list[dict]:
    """Pro každé adoptované zařízení lokality spočítá rozdíl proti politice
    a vytvoří plán (draft). Vrací přehled včetně zařízení beze změn."""
    out = []
    with tenant_scope(db, tenant_id) as tx:
        policy = get_site_policy(tx, site_id, "ntp")
        if not policy:
            raise ServiceError("lokalita nemá nastavenou NTP politiku")
        devs = tx.fetchall(
            """SELECT id, serial, identity FROM device WHERE site_id = %(s)s
               AND state IN ('adopted', 'offline') ORDER BY serial""", {"s": str(site_id)})
        invs = {str(d["id"]): inventory.load(tx, d["id"]) for d in devs}
        primary_inv = invs.get(policy["primary_device_id"])
        for d in devs:
            did = str(d["id"])
            inv = invs[did]
            item = {"device_id": did, "serial": d["serial"], "identity": d["identity"]}
            if not inv:
                out.append(item | {"result": "skipped", "reason": "inventura zatím neproběhla"})
                continue
            desired, reason = ntp.desired_for(policy, did, inv, primary_inv)
            if desired is None:
                out.append(item | {"result": "skipped", "reason": reason})
                continue
            chg = ntp.changes(desired, inv)
            tx.execute(
                """UPDATE config_plan SET status = 'superseded'
                   WHERE device_id = %(d)s AND module = 'ntp' AND status = 'draft'""", {"d": did})
            if not chg:
                out.append(item | {"result": "ok"})
                continue
            row = tx.fetchone(
                """INSERT INTO config_plan (tenant_id, device_id, module, summary, changes, script, basis_sha, created_by)
                   VALUES (%(t)s, %(d)s, 'ntp', %(sum)s, %(chg)s, %(sc)s, %(b)s, %(u)s) RETURNING id""",
                {"t": str(tenant_id), "d": did, "sum": ntp.summary(chg), "chg": chg,
                 "sc": ntp.script(desired, chg), "b": inventory.basis_sha(inv, ntp.SECTIONS), "u": _s(user_id)})
            out.append(item | {"result": "plan", "plan_id": row["id"], "summary": ntp.summary(chg)})
        audit(tx, "plan.generate", tenant_id=tenant_id, user_id=user_id, target=f"site:{site_id}",
              after={"module": "ntp", "plans": sum(1 for o in out if o["result"] == "plan")}, ip=ip)
    return out


def site_snooping(tx: Tx, site_id: Any) -> tuple[dict, list[dict], dict]:
    """Analýza DHCP snoopingu lokality z aktuálních inventur: (výsledek, zařízení, inventury)."""
    devs = tx.fetchall(
        """SELECT id, serial, identity, model, role::text AS role, ros_version FROM device
           WHERE site_id = %(s)s AND state IN ('adopted', 'offline') ORDER BY identity, serial""", {"s": str(site_id)})
    invs = {str(d["id"]): inventory.load(tx, d["id"]) for d in devs}
    clients = {r["ip"]: r for r in tx.fetchall(
        """SELECT DISTINCT ON (ip) ip, device_id::text AS device_id, port FROM client
           WHERE site_id = %(s)s AND ip IS NOT NULL AND last_seen >= now() - interval '7 days'
           ORDER BY ip, last_seen DESC""", {"s": str(site_id)})}
    policy = get_site_policy(tx, site_id, "dhcp_snooping") or {}
    return dhcpguard.site_snooping(devs, invs, clients, policy), devs, invs


def generate_snooping_plans(db: Database, tenant_id: Any, site_id: Any, user_id: Any = None,
                            ip: str | None = None) -> dict:
    """Plány DHCP snoopingu (draft) pro zařízení lokality se známou cestou ke všem DHCP zdrojům."""
    with tenant_scope(db, tenant_id) as tx:
        res, devs, invs = site_snooping(tx, site_id)
        n = 0
        for item in res["devices"]:
            did = item["device_id"]
            tx.execute("""UPDATE config_plan SET status = 'superseded'
                          WHERE device_id = %(d)s AND module = 'dhcp_snooping' AND status = 'draft'""", {"d": did})
            if item["status"] != "plan":
                continue
            row = tx.fetchone(
                """INSERT INTO config_plan (tenant_id, device_id, module, summary, changes, script, revert_script,
                                            confirm_minutes, basis_sha, created_by)
                   VALUES (%(t)s, %(d)s, 'dhcp_snooping', %(sum)s, %(chg)s, %(sc)s, %(rv)s, %(cm)s, %(b)s, %(u)s)
                   RETURNING id""",
                {"t": str(tenant_id), "d": did, "sum": "DHCP snooping: " + item["summary"],
                 "chg": {"trusted": item["trusted"], "bridges": item["bridges"], "alerts": item["alerts"],
                         "keep": item["keep"], "warnings": item["warnings"]},
                 "sc": item["apply"], "rv": item["revert"], "cm": CONFIRM_MINUTES,
                 "b": dhcpguard.basis(invs[did]), "u": _s(user_id)})
            item["plan_id"] = row["id"]
            n += 1
        audit(tx, "plan.generate", tenant_id=tenant_id, user_id=user_id, target=f"site:{site_id}",
              after={"module": "dhcp_snooping", "plans": n}, ip=ip)
    return res


def approve_plan(db: Database, settings: Settings, tenant_id: Any, plan_id: int, user_id: Any,
                 ip: str | None = None) -> int:
    superseded = False
    with tenant_scope(db, tenant_id) as tx:
        p = tx.fetchone(
            """SELECT p.id, p.device_id, p.module, p.status, p.basis_sha, p.created_by, d.state, d.managed_at
               FROM config_plan p JOIN device d ON d.id = p.device_id
               WHERE p.id = %(i)s FOR UPDATE OF p""", {"i": plan_id})
        if not p:
            raise NotFound("plán neexistuje")
        if p["status"] != "draft":
            raise ServiceError(f"plán není ve stavu draft (je {p['status']})")
        if p["module"] == "vlan":
            raise ServiceError("plány VLAN jsou zatím jen náhled")
        if p["module"] == "takeover":
            if p["managed_at"] is not None:
                raise ServiceError("zařízení už je řízené")
            if p["state"] != "adopted":
                raise ServiceError("převzetí lze spustit jen u zařízení, které je online")
        elif p["module"] in ("switch", "capsman", "wifitable") and p["managed_at"] is not None:
            pass  # řízené zařízení: konfiguraci určuje záměr lokality
        elif p["module"] == "hardening":
            pass  # oprava bezpečnostního nálezu – výslovná volba uživatele, s automatickým návratem
        elif p["module"] == "switch":
            raise ServiceError("sladění se záměrem je jen pro řízená zařízení")
        elif module_modes(tx, p["device_id"]).get(p["module"]) != "manage":
            raise ServiceError(f"modul {p['module']} je na zařízení v režimu observe – nejdřív přepni na manage")
        if p["state"] not in ("adopted", "offline"):
            raise ServiceError("zařízení není adoptované")
        if p["module"] in settings.security.four_eyes_modules and str(p["created_by"]) == str(user_id):
            raise ServiceError("tento plán musí schválit jiný uživatel než autor")
        inv = inventory.load(tx, p["device_id"])
        if basis_for(p["module"], inv) != p["basis_sha"]:
            tx.execute("UPDATE config_plan SET status = 'superseded' WHERE id = %(i)s", {"i": plan_id})
            superseded = True
        else:
            cid = enqueue(tx, tenant_id, p["device_id"], "apply_plan", {"plan_id": plan_id}, user_id)
            tx.execute("UPDATE device_command SET plan_id = %(p)s WHERE id = %(c)s", {"p": plan_id, "c": cid})
            tx.execute(
                """UPDATE config_plan SET status = 'queued', approved_by = %(u)s, approved_at = now(),
                       command_id = %(c)s WHERE id = %(i)s""", {"u": _s(user_id), "c": cid, "i": plan_id})
            audit(tx, "plan.approve", tenant_id=tenant_id, user_id=user_id, target=f"device:{p['device_id']}",
                  after={"plan_id": plan_id, "module": p["module"], "command_id": cid}, ip=ip)
    if superseded:
        raise ServiceError("konfigurace zařízení se od vytvoření plánu změnila – vygeneruj plán znovu")
    return cid


def discard_plan(db: Database, tenant_id: Any, plan_id: int, user_id: Any, ip: str | None = None) -> None:
    with tenant_scope(db, tenant_id) as tx:
        n = tx.execute("UPDATE config_plan SET status = 'discarded' WHERE id = %(i)s AND status = 'draft'",
                       {"i": plan_id})
        if not n:
            raise ServiceError("plán nelze zahodit")
        audit(tx, "plan.discard", tenant_id=tenant_id, user_id=user_id, target=f"plan:{plan_id}", ip=ip)


def create_port_plan(db: Database, tenant_id: Any, device_id: Any, port: str, desired: dict,
                     user_id: Any = None, ip: str | None = None, uplinks: set[str] | None = None) -> dict:
    """Plán změny portu (draft) s návratovým skriptem; předchozí draft pro stejný port zruší."""
    with tenant_scope(db, tenant_id) as tx:
        if not tx.fetchone("SELECT 1 FROM device WHERE id = %(d)s AND state IN ('adopted', 'offline')",
                           {"d": str(device_id)}):
            raise NotFound("zařízení neexistuje nebo není adoptované")
        inv = inventory.load(tx, device_id)
        if not inv:
            raise ServiceError("inventura zařízení zatím neproběhla")
        try:
            p = portcfg.plan(inv, port, desired, uplinks)
        except portcfg.PortError as e:
            raise ServiceError(str(e)) from e
        if not p["changes"]:
            raise ServiceError("žádná změna oproti aktuálnímu stavu")
        tx.execute("""UPDATE config_plan SET status = 'superseded' WHERE device_id = %(d)s AND module = 'ports'
                      AND status = 'draft' AND changes->>'port' = %(p)s""", {"d": str(device_id), "p": port})
        row = tx.fetchone(
            """INSERT INTO config_plan (tenant_id, device_id, module, summary, changes, script, revert_script,
                                        confirm_minutes, basis_sha, created_by)
               VALUES (%(t)s, %(d)s, 'ports', %(sum)s, %(chg)s, %(sc)s, %(rv)s, %(cm)s, %(b)s, %(u)s) RETURNING id""",
            {"t": str(tenant_id), "d": str(device_id), "sum": p["summary"],
             "chg": {"port": port, "changes": p["changes"], "warnings": p["warnings"]},
             "sc": p["apply"], "rv": p["revert"], "cm": CONFIRM_MINUTES, "b": portcfg.config_basis(inv),
             "u": _s(user_id)})
        audit(tx, "plan.create", tenant_id=tenant_id, user_id=user_id, target=f"device:{device_id}",
              after={"plan_id": row["id"], "module": "ports", "summary": p["summary"]}, ip=ip)
    return {"plan_id": row["id"], "summary": p["summary"], "warnings": p["warnings"], "changes": p["changes"]}


def _ports_ready(tx: Tx, device_id: Any) -> dict:
    if not tx.fetchone("SELECT 1 FROM device WHERE id = %(d)s AND state IN ('adopted', 'offline')",
                       {"d": str(device_id)}):
        raise NotFound("zařízení neexistuje nebo není adoptované")
    inv = inventory.load(tx, device_id)
    if not inv:
        raise ServiceError("inventura zařízení zatím neproběhla")
    return inv


def _insert_ports_plan(tx: Tx, tenant_id: Any, device_id: Any, inv: dict, p: dict, chg: dict,
                       user_id: Any, ip: str | None) -> dict:
    if not p["changes"]:
        raise ServiceError("žádná změna oproti aktuálnímu stavu")
    row = tx.fetchone(
        """INSERT INTO config_plan (tenant_id, device_id, module, summary, changes, script, revert_script,
                                    confirm_minutes, basis_sha, created_by)
           VALUES (%(t)s, %(d)s, 'ports', %(sum)s, %(chg)s, %(sc)s, %(rv)s, %(cm)s, %(b)s, %(u)s) RETURNING id""",
        {"t": str(tenant_id), "d": str(device_id), "sum": p["summary"][:500],
         "chg": {**chg, "changes": p["changes"], "warnings": p["warnings"]},
         "sc": p["apply"], "rv": p["revert"], "cm": CONFIRM_MINUTES, "b": portcfg.config_basis(inv),
         "u": _s(user_id)})
    audit(tx, "plan.create", tenant_id=tenant_id, user_id=user_id, target=f"device:{device_id}",
          after={"plan_id": row["id"], "module": "ports", "summary": p["summary"][:500]}, ip=ip)
    return {"plan_id": row["id"], "summary": p["summary"], "warnings": p["warnings"], "changes": p["changes"]}


def create_ports_plan(db: Database, tenant_id: Any, device_id: Any, items: dict[str, dict], user_id: Any = None,
                      ip: str | None = None, uplinks: set[str] | None = None, warnings: list[str] | None = None) -> dict:
    """Jeden plán pro víc portů zařízení (hromadná úprava, profil na víc portů)."""
    with tenant_scope(db, tenant_id) as tx:
        inv = _ports_ready(tx, device_id)
        try:
            p = portcfg.plan_many(inv, items, uplinks)
        except portcfg.PortError as e:
            raise ServiceError(str(e)) from e
        p["warnings"] = list(warnings or []) + p["warnings"]
        return _insert_ports_plan(tx, tenant_id, device_id, inv, p, {"ports": sorted(items)}, user_id, ip)


def create_bond_plan(db: Database, tenant_id: Any, device_id: Any, kind: str, params: dict, user_id: Any = None,
                     ip: str | None = None, uplinks: set[str] | None = None) -> dict:
    """LACP bond z portů, zrušení bondu controlleru, nebo MLAG (jeden switch dvojice)."""
    from . import bondcfg

    with tenant_scope(db, tenant_id) as tx:
        inv = _ports_ready(tx, device_id)
        try:
            if kind == "lacp":
                vlan = params.get("vlan") if isinstance(params.get("vlan"), dict) else None
                if params.get("profile_id") not in (None, ""):
                    from . import profiles

                    prof = profiles.expand(profiles.get(tx, int(params["profile_id"])), profiles.site_vlans(tx, device_id))
                    vlan = {k: prof.get(k) for k in ("pvid", "tagged", "edge") if prof.get(k) is not None}
                p = bondcfg.lacp_plan(inv, [str(x) for x in params.get("ports") or []],
                                      lacp_rate=str(params.get("lacp_rate") or "30secs"),
                                      hash_policy=str(params.get("hash") or "layer-2-and-3"), uplinks=uplinks,
                                      vlan=vlan)
            elif kind == "site_vlans":
                from . import takeover, vlanprep

                srow = tx.fetchone("SELECT site_id FROM device WHERE id = %(d)s", {"d": str(device_id)})
                intent = takeover.load_intent(tx, srow["site_id"]) if srow and srow["site_id"] else None
                p = vlanprep.plan(inv, intent, uplinks)
            elif kind == "bond_remove":
                p = bondcfg.remove_plan(inv, str(params.get("name") or ""))
            elif kind == "mlag":
                ver = (tx.fetchone("SELECT ros_version FROM device WHERE id = %(d)s", {"d": str(device_id)}) or {}).get("ros_version")
                p = bondcfg.mlag_plan(inv, ver or "", str(params.get("peer_port") or ""), params.get("members") or [],
                                      primary=bool(params.get("primary")))
                p["warnings"] = list(params.get("pair_warnings") or []) + p["warnings"]
            else:
                raise ServiceError("neznámý druh agregace")
        except portcfg.PortError as e:
            raise ServiceError(str(e)) from e
        return _insert_ports_plan(tx, tenant_id, device_id, inv, p, {"bond": kind}, user_id, ip)


def plan_script(tx: Tx, plan_id: int, device_id: Any) -> dict:
    r = tx.fetchone("""SELECT module, script, revert_script, confirm_minutes FROM config_plan
                       WHERE id = %(i)s AND device_id = %(d)s AND status = 'queued'""",
                    {"i": plan_id, "d": str(device_id)})
    if not r:
        raise ServiceError("plán k aplikaci neexistuje")
    return r


def finish_plan(tx: Tx, command_id: int, ok: bool) -> None:
    """Výsledek aplikace. Plán s návratovým skriptem čeká na potvrzení (confirming)."""
    tx.execute(
        """UPDATE config_plan SET finished_at = now(),
               status = CASE WHEN NOT %(ok)s THEN 'failed'::plan_status
                             WHEN revert_script IS NOT NULL THEN 'confirming'::plan_status
                             ELSE 'applied'::plan_status END,
               confirm_deadline = CASE WHEN %(ok)s AND revert_script IS NOT NULL
                                       THEN now() + make_interval(mins => confirm_minutes) END
           WHERE command_id = %(c)s AND status = 'queued'""",
        {"ok": ok, "c": command_id})


def pending_confirm(tx: Tx, device_id: Any) -> dict | None:
    """Plán čekající na potvrzení, pro který ještě není ve frontě potvrzení."""
    return tx.fetchone(
        """SELECT p.id, p.tenant_id, p.module, (p.confirm_deadline < now()) AS overdue FROM config_plan p WHERE p.device_id = %(d)s AND p.status = 'confirming'
             AND NOT EXISTS (SELECT 1 FROM device_command c WHERE c.plan_id = p.id AND c.kind = 'confirm_plan'
                             AND c.status IN ('queued', 'sent'))
           ORDER BY p.id LIMIT 1""", {"d": str(device_id)})


def confirm_done(tx: Tx, plan_id: int, ok: bool, tenant_id: Any = None) -> None:
    module = (tx.fetchone("SELECT module FROM config_plan WHERE id = %(i)s", {"i": plan_id}) or {}).get("module")
    if ok:
        n = tx.execute("""UPDATE config_plan SET status = 'applied', confirmed_at = now()
                          WHERE id = %(i)s AND status = 'confirming'""", {"i": plan_id})
        if n and module == "takeover":
            from . import takeover

            takeover.finished(tx, tenant_id, plan_id, True)
    elif module == "takeover":
        # potvrzení převzetí selhalo = na zařízení neběží bootstrap (vrátilo se ze zálohy)
        from . import takeover

        takeover.finished(tx, tenant_id, plan_id, False)


def rolled_back(tx: Tx, command_id: int) -> dict | None:
    return tx.fetchone(
        """UPDATE config_plan SET status = 'rolled_back', finished_at = now()
           WHERE command_id = %(c)s AND status IN ('confirming', 'queued', 'applied', 'failed')
           RETURNING id, device_id, summary""",
        {"c": command_id})


def create_lease_plan(db: Database, tenant_id: Any, device_id: Any, server: str, mac: str, desired: dict,
                      user_id: Any = None, ip: str | None = None) -> dict:
    """Plán změny DHCP leasu (rezervace/statická IP, options, …) s návratovým skriptem."""
    with tenant_scope(db, tenant_id) as tx:
        if not tx.fetchone("SELECT 1 FROM device WHERE id = %(d)s AND state IN ('adopted', 'offline')",
                           {"d": str(device_id)}):
            raise NotFound("zařízení neexistuje nebo není adoptované")
        inv = inventory.load(tx, device_id)
        if not inv:
            raise ServiceError("inventura zařízení zatím neproběhla")
        try:
            p = leases.plan(inv, server, mac, desired)
        except ValueError as e:
            raise ServiceError(str(e)) from e
        if not p["changes"]:
            raise ServiceError("žádná změna oproti aktuálnímu stavu")
        m = leases.norm_mac(mac)
        tx.execute("""UPDATE config_plan SET status = 'superseded' WHERE device_id = %(d)s AND module = 'dhcp'
                      AND status = 'draft' AND changes->>'mac' = %(m)s AND changes->>'server' = %(s)s""",
                   {"d": str(device_id), "m": m, "s": server})
        row = tx.fetchone(
            """INSERT INTO config_plan (tenant_id, device_id, module, summary, changes, script, revert_script,
                                        confirm_minutes, basis_sha, created_by)
               VALUES (%(t)s, %(d)s, 'dhcp', %(sum)s, %(chg)s, %(sc)s, %(rv)s, %(cm)s, %(b)s, %(u)s) RETURNING id""",
            {"t": str(tenant_id), "d": str(device_id), "sum": p["summary"],
             "chg": {"server": server, "mac": m, "changes": p["changes"], "warnings": p["warnings"]},
             "sc": p["apply"], "rv": p["revert"], "cm": CONFIRM_MINUTES, "b": leases.config_basis(inv),
             "u": _s(user_id)})
        audit(tx, "plan.create", tenant_id=tenant_id, user_id=user_id, target=f"device:{device_id}",
              after={"plan_id": row["id"], "module": "dhcp", "server": server, "mac": m, "summary": p["summary"]}, ip=ip)
    return {"plan_id": row["id"], "summary": p["summary"], "changes": p["changes"], "warnings": p["warnings"],
            "script": p["apply"]}


def create_network_plan(db: Database, tenant_id: Any, device_id: Any, server: str, desired: dict,
                        user_id: Any = None, ip: str | None = None) -> dict:
    """Plán změny nastavení sítě DHCP (DNS, NTP, doména, options pro všechny klienty) s návratem."""
    with tenant_scope(db, tenant_id) as tx:
        if not tx.fetchone("SELECT 1 FROM device WHERE id = %(d)s AND state IN ('adopted', 'offline')",
                           {"d": str(device_id)}):
            raise NotFound("zařízení neexistuje nebo není adoptované")
        inv = inventory.load(tx, device_id)
        if not inv:
            raise ServiceError("inventura zařízení zatím neproběhla")
        try:
            p = leases.network_plan(inv, server, desired)
        except ValueError as e:
            raise ServiceError(str(e)) from e
        if not p["changes"]:
            raise ServiceError("žádná změna oproti aktuálnímu stavu")
        tx.execute("""UPDATE config_plan SET status = 'superseded' WHERE device_id = %(d)s AND module = 'dhcp'
                      AND status = 'draft' AND changes->>'network' = %(s)s""", {"d": str(device_id), "s": server})
        row = tx.fetchone(
            """INSERT INTO config_plan (tenant_id, device_id, module, summary, changes, script, revert_script,
                                        confirm_minutes, basis_sha, created_by)
               VALUES (%(t)s, %(d)s, 'dhcp', %(sum)s, %(chg)s, %(sc)s, %(rv)s, %(cm)s, %(b)s, %(u)s) RETURNING id""",
            {"t": str(tenant_id), "d": str(device_id), "sum": p["summary"],
             "chg": {"server": server, "network": server, "changes": p["changes"], "warnings": p["warnings"]},
             "sc": p["apply"], "rv": p["revert"], "cm": CONFIRM_MINUTES, "b": leases.config_basis(inv),
             "u": _s(user_id)})
        audit(tx, "plan.create", tenant_id=tenant_id, user_id=user_id, target=f"device:{device_id}",
              after={"plan_id": row["id"], "module": "dhcp", "server": server, "summary": p["summary"]}, ip=ip)
    return {"plan_id": row["id"], "summary": p["summary"], "changes": p["changes"], "warnings": p["warnings"],
            "script": p["apply"]}


def set_client_note(db: Database, tenant_id: Any, mac: str, note: str, user_id: Any = None,
                    ip: str | None = None) -> dict:
    """Jen poznámka (zpětná kompatibilita) – popisek a ikonu řeší clientmeta.set_client."""
    from .clientmeta import set_client

    return set_client(db, tenant_id, mac, {"note": note}, user_id, ip)


def create_hardening_plan(db: Database, tenant_id: Any, device_id: Any, codes: list[str], user_id: Any = None,
                          ip: str | None = None) -> dict:
    """Plán opravy bezpečnostních nálezů (vypnutí nešifrovaných služeb, proxy…) s automatickým návratem.
    Oprava je výslovná akce uživatele, proto nepotřebuje režim „správa“ modulu."""
    from . import hardening

    if not isinstance(codes, list) or not codes or not all(isinstance(c, str) for c in codes):
        raise ServiceError("vyber nálezy k opravě")
    with tenant_scope(db, tenant_id) as tx:
        dev = tx.fetchone("SELECT state FROM device WHERE id = %(d)s", {"d": str(device_id)})
        if not dev:
            raise NotFound("zařízení neexistuje")
        if dev["state"] not in ("adopted", "offline"):
            raise ServiceError("opravu lze poslat jen adoptovanému zařízení")
        inv = inventory.load(tx, device_id)
        try:
            apply, revert, labels = hardening.fix_scripts(codes, inv.get("hardening") or [])
        except ValueError as e:
            raise ServiceError(str(e)) from e
        tx.execute("""UPDATE config_plan SET status = 'superseded' WHERE device_id = %(d)s
                      AND module = 'hardening' AND status = 'draft'""", {"d": str(device_id)})
        row = tx.fetchone(
            """INSERT INTO config_plan (tenant_id, device_id, module, summary, changes, script, revert_script,
                                        confirm_minutes, basis_sha, created_by)
               VALUES (%(t)s, %(d)s, 'hardening', %(sum)s, %(chg)s, %(sc)s, %(rv)s, %(cm)s, %(b)s, %(u)s) RETURNING id""",
            {"t": str(tenant_id), "d": str(device_id), "sum": "Bezpečnost: " + ", ".join(labels),
             "chg": {"codes": codes, "labels": labels}, "sc": apply, "rv": revert, "cm": CONFIRM_MINUTES,
             "b": basis_for("hardening", inv), "u": _s(user_id)})
        audit(tx, "plan.create", tenant_id=tenant_id, user_id=user_id, target=f"device:{device_id}",
              after={"plan_id": row["id"], "module": "hardening", "labels": labels}, ip=ip)
    return {"plan_id": row["id"], "labels": labels}
