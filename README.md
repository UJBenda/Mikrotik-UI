# MikroTik Controller

Vlastní multi-tenant controller pro MikroTik RouterOS v7 ve stylu UniFi:
centrální správa, topologická mapa, sledování klientů, VLAN, provoz,
zálohy a firmware. Zařízení se připojují jen odchozím spojením na 443
(SSTP tunel + HTTPS check-in), takže fungují i za NATem a CGNATem.

Návrh architektury: [docs/architecture.md](docs/architecture.md).
Instalace na Debian: [docs/install.md](docs/install.md).

## Stav

Fáze **F0 – Základ** (rozpracováno):

- [x] Kostra projektu, konfigurace, systemd unity, nginx
- [x] Datový model: tenanti, lokality, zařízení, tajemství, uživatelé, role, audit
- [x] Row-level security mezi tenanty, append-only audit, jednorázové adopční tokeny
- [x] Šifrování tajemství (envelope, AES-256-GCM), hesla (scrypt), tokeny
- [x] Přidělování tunelových IP (IPAM)
- [x] Abstrakce koncentrátoru + provider pro fyzický MikroTik
- [ ] Adopce zařízení (bootstrap skript, schválení, provisioning)
- [ ] HTTPS check-in (HMAC, fronta příkazů, upload záloh)
- [ ] Přihlášení, MFA, RBAC v API
- [ ] Vlastní CA pro klientské certifikáty

## Struktura

```
src/mtcontroller/
  config.py        konfigurace (config.toml + MTC_* proměnné)
  db.py            připojení, tenant_scope / operator_scope pro RLS
  models.py        ORM modely
  security.py      hesla, tokeny, HMAC, šifrování tajemství
  ipam.py          tunelové adresy
  tunnel/          abstrakce koncentrátoru (MikroTik, později RADIUS)
  web/             FastAPI (admin UI + check-in, oddělené podle hostname)
  worker/          polling, fronta změn, zálohy
  cli.py           mtc-admin
migrations/        Alembic; SQL schéma v migrations/sql/
deploy/            systemd unity, nginx
tests/             pytest + tests/db (kontroly schématu proti Postgresu)
```

## Vývoj

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest

# kontroly schématu (RLS, audit, tokeny) proti lokálnímu Postgresu 16+
sudo -u postgres tests/db/run.sh
```
