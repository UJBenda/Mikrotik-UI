"""Uložit = nasadit (jako UniFi): změny záměru lokality se na řízená zařízení pošlou hned.

Po uložení sítí, Wi-Fi, skupin AP, výjimek zařízení nebo portu řízeného switche se spočítá
rozdíl (switchcfg.reconcile, CAPsMAN), plán se rovnou schválí pod jménem uživatele a zařízení
se probudí (kick). Plány zůstávají jako žurnál a commit-confirm s automatickým návratem platí
dál – na zařízení se nic nemění jinak než dřív, jen bez ručního „Schválit“.

Plán zůstane ke schválení, když:
- uživatel nemá právo schvalovat (technik) – výsledek `draft`,
- modul je v `security.four_eyes_modules` – schválit musí jiný správce.
Sledovaná zařízení (ne řízená) se nemění nikdy.
"""

from __future__ import annotations

from typing import Any

from . import intent, inventory, portcfg, switchcfg, takeover
from .config import Settings
from .db import Database, tenant_scope
from .plans import approve_plan
from .security import SecretBox
from .services import ServiceError, _s, audit, kick

VLAN_KEYS = ("pvid", "tagged", "disabled")


def _approve(db: Database, settings: Settings, tenant_id: Any, item: dict, can_approve: bool,
             user_id: Any, ip: str | None) -> None:
    if not can_approve:
        item.update(result="draft", reason="schválí správce tenanta (Aktivita → Změny)")
        return
    try:
        item["command_id"] = approve_plan(db, settings, tenant_id, item["plan_id"], user_id, ip)
        item["result"] = "applying"
    except ServiceError as e:
        item.update(result="draft", reason=str(e))


def _wifi(db: Database, settings: Settings, box: SecretBox, tenant_id: Any, site_id: Any, can_approve: bool,
          user_id: Any, ip: str | None) -> dict | None:
    """CAPsMAN lokality: nový plán jen když se skript liší od posledního nasazeného."""
    with tenant_scope(db, tenant_id) as tx:
        cur = takeover.load_intent(tx, site_id) or {}
        host = (cur.get("wifi") or {}).get("capsman_device_id")
        if not host:
            return None
        last = tx.fetchone("""SELECT script FROM config_plan WHERE device_id = %(d)s AND module = 'capsman'
                              AND status IN ('queued', 'confirming', 'applied') ORDER BY id DESC LIMIT 1""", {"d": host})
        if not last and not takeover.list_ssids(tx, site_id):
            return None  # Wi-Fi na lokalitě zatím není
        try:
            w = takeover.wifi_preview(tx, site_id)
        except ServiceError as e:
            return {"device_id": host, "result": "blocked", "reason": str(e)}
        name = w["host"]["identity"] or w["host"]["serial"]
        if w["blockers"]:
            return {"device_id": host, "device": name, "result": "blocked", "reason": "; ".join(w["blockers"])}
        if last and last["script"] == w["script"]:
            return {"device_id": host, "device": name, "result": "ok"}
    try:
        res = takeover.create_wifi_plan(db, box, tenant_id, site_id, user_id, ip)
    except ServiceError as e:
        return {"device_id": host, "device": name, "result": "blocked", "reason": str(e)}
    item = {"device_id": host, "device": name, "result": "plan", "plan_id": res["plan_id"], "summary": res["summary"]}
    _approve(db, settings, tenant_id, item, can_approve, user_id, ip)
    return item


def site(db: Database, settings: Settings, box: SecretBox, tenant_id: Any, site_id: Any, user_id: Any = None,
         ip: str | None = None, *, can_approve: bool = True, switches: bool = True, wifi: bool = True,
         only: set[str] | None = None) -> dict:
    """Sladí řízená zařízení lokality se záměrem a rovnou nasadí. Vrací přehled pro UI:
    {"devices": [{device_id, device, result: ok|applying|draft|blocked|skipped, …}], "wifi": {…} | None}."""
    out: dict[str, Any] = {"devices": [], "wifi": None}
    with tenant_scope(db, tenant_id) as tx:
        if not takeover.load_intent(tx, site_id):
            return out
    if switches:
        res = takeover.generate_switch_plans(db, settings, tenant_id, site_id, user_id, ip, auto=False, only=only)
        for item in res:
            if item["result"] == "plan":
                _approve(db, settings, tenant_id, item, can_approve, user_id, ip)
        out["devices"] = res
    if wifi:
        out["wifi"] = _wifi(db, settings, box, tenant_id, site_id, can_approve, user_id, ip)
    for item in out["devices"] + ([out["wifi"]] if out["wifi"] else []):
        if item.get("command_id"):
            kick(db, settings, box, tenant_id, item["device_id"])
    return out


def is_managed(db: Database, tenant_id: Any, device_id: Any) -> bool:
    with tenant_scope(db, tenant_id) as tx:
        return tx.fetchone("SELECT 1 FROM device WHERE id = %(d)s AND managed_at IS NOT NULL",
                           {"d": str(device_id)}) is not None


def override_for(desired: dict, state: dict, site_vids: list[int]) -> dict | None:
    """Port řízeného switche: požadované VLAN z editoru / profilu → výjimka záměru zařízení.
    None = VLAN portu se nemění. Všechny sítě lokality tagovaně = trunk (jako vestavěný profil)."""
    disabled = bool(desired.get("disabled", state.get("disabled")))
    pvid = int(desired.get("pvid") or state.get("pvid") or 1)
    raw = desired["tagged"] if "tagged" in desired else state.get("tagged") or []
    tagged = sorted({int(x) for x in raw if str(x).isdigit()} - {pvid})
    same = (disabled == bool(state.get("disabled")) and pvid == int(state.get("pvid") or 1)
            and tagged == sorted({int(x) for x in state.get("tagged") or [] if str(x).isdigit()} - {pvid}))
    if same:
        return None
    if disabled:
        return {"mode": "off"}
    others = [v for v in site_vids if v != pvid]
    if tagged and others and set(others) <= set(tagged):
        return {"mode": "trunk"}
    if not tagged:
        return {"mode": "access", "vlan": pvid}
    return {"mode": "hybrid", "vlan": pvid, "tagged": tagged}


def port_vlans(db: Database, settings: Settings, box: SecretBox, tenant_id: Any, device_id: Any,
               items: dict[str, dict], user_id: Any = None, ip: str | None = None, *, can_approve: bool = True) -> dict | None:
    """VLAN portů řízeného zařízení: uloží výjimky do záměru zařízení a sladí (jen toto zařízení).
    None = žádná VLAN změna. Změna, kterou by model odmítl (např. uplink jako access), se neuloží."""
    with tenant_scope(db, tenant_id) as tx:
        d = takeover._device(tx, device_id)
        if d["managed_at"] is None:
            raise ServiceError("zařízení není řízené")
        site_intent = takeover.load_intent(tx, d["site_id"]) if d["site_id"] else None
        if not site_intent:
            raise ServiceError("lokalita nemá sítě")
        inv = inventory.load(tx, device_id)
        if not inv:
            raise ServiceError("inventura zařízení zatím neproběhla")
        vids = [int(n["vlan_id"]) for n in site_intent["networks"]]
        old = takeover.load_device_intent(tx, device_id)
        ports = dict(old["ports"] or {})
        changed = []
        for port, desired in items.items():
            try:
                st = portcfg.port_state(inv, port)
            except portcfg.PortError as e:
                raise ServiceError(str(e)) from e
            o = override_for(desired, st, vids)
            if o is not None:
                ports[port] = o
                changed.append(port)
        if not changed:
            return None
        names = {p["name"] for p in switchcfg.physical_ports(inv)}
        try:
            new = intent.validate_device({"ports": ports, "mgmt": old["mgmt"] or {}, "role": old["role"]}, site_intent, names or None)
        except intent.IntentError as e:
            raise ServiceError(str(e)) from e
        if not (old["mgmt"] or {}).get("mode"):
            new["mgmt"] = {}
        before, _x = takeover._model(tx, d, inv)
        model, _di = takeover._model(tx, d, inv, new)
        bad = [b for b in model["blockers"] if b not in before["blockers"]]
        if bad:
            raise ServiceError("změnu nelze: " + "; ".join(bad))
        tx.execute(
            """INSERT INTO device_intent (tenant_id, device_id, ports, mgmt, role, updated_by)
               VALUES (%(t)s, %(d)s, %(p)s, %(m)s, %(r)s, %(u)s)
               ON CONFLICT (device_id) DO UPDATE SET ports = EXCLUDED.ports, updated_by = EXCLUDED.updated_by,
                   updated_at = now()""",
            {"t": str(tenant_id), "d": str(device_id), "p": new["ports"], "m": new["mgmt"], "r": new["role"],
             "u": _s(user_id)})
        audit(tx, "device.intent", tenant_id=tenant_id, user_id=user_id, target=f"device:{device_id}",
              before={"ports": {p: (old["ports"] or {}).get(p) for p in changed}},
              after={"ports": {p: ports[p] for p in changed}}, ip=ip)
        site_id = d["site_id"]
    return site(db, settings, box, tenant_id, site_id, user_id, ip, can_approve=can_approve, wifi=False,
                only={str(device_id)})


def split(desired: dict) -> tuple[dict, dict]:
    """Požadavek portu → (VLAN část pro záměr, zbytek pro plán portu: PoE, popis, edge, hlídač…)."""
    return ({k: v for k, v in desired.items() if k in VLAN_KEYS},
            {k: v for k, v in desired.items() if k not in VLAN_KEYS})
