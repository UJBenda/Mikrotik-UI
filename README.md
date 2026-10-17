# MikroTik Controller

Vlastní multi-tenant controller pro MikroTik RouterOS v7 ve stylu UniFi:
centrální správa, topologická mapa, sledování klientů, VLAN, provoz,
zálohy a firmware. Zařízení se připojují jen odchozím spojením na 443
(SSTP tunel + HTTPS check-in), takže fungují i za NATem a CGNATem.

Návrh architektury: [docs/architecture.md](docs/architecture.md).
Instalace na Debian: [docs/install.md](docs/install.md).

## Stav

Fáze **F0 – Základ**: připraveno k prvnímu nasazení ([testovací plán](docs/test-plan.md)).

- [x] Instalace na Debian 13 jen z apt (`deploy/install.sh`), systemd sandbox, nginx
- [x] Multi-tenant datový model, row-level security, append-only audit
- [x] Admin UI + API: přihlášení, povinné TOTP MFA, zamykání účtu, CSRF, role per tenant
- [x] Adopce jedním řádkem v terminálu MikroTiku, schválení v UI
- [x] Check-in přes HTTPS, fronta příkazů s potvrzením (provisioning, restart, odebrání)
- [x] SSTP tunel na každé zařízení, účty na koncentrátoru přes REST s pinningem certifikátu
- [x] Ověření adopce přes REST v tunelu, offline detekce, failover mezi koncentrátory
- [x] Generátor konfigurace koncentrátoru (`mtc-admin concentrator-script`)
- [x] Inventura přes REST (jen čtení), metriky rozhraní (5 min / hodinové agregace)
- [x] Zálohy konfigurace (šifrovaně), drift, diff se skrytými hesly
- [x] Analýza VLAN/bridge proti cílovému modelu + náhled migrace
- [x] Modul NTP s plány změn (observe/manage, schválení, zneplatnění)
- [x] Topologie lokality (MNDP/LLDP), dostupné aktualizace RouterOS, známé problémy verzí
- [x] Aktualizace RouterOS a RouterBOOT z UI (potvrzená verze, blokace kritických verzí, kontrola výsledku)
- [x] Release notes: changelogy mezilehlých verzí, změny relevantní pro konkrétní zařízení
- [x] `mtc-admin doctor` – kontrola instalace
- [ ] Vlastní CA a klientské certifikáty pro SSTP
- [x] Klienti lokality (DHCP/ARP/bridge host/wifi) s polohou na portu/AP
- [x] Porty (rychlost, duplex, SFP, PoE, VLAN, STP role) a STP lokality (root, blokované porty, rizika smyček)
- [x] Wi-Fi lokality: rádia (wifi i wireless), SSID, kanály, klienti, signál, CAPsMAN AP, varování
- [x] Alerty (offline, tunel, REST, certifikát, CPU/paměť, porty, zálohy, verze) a notifikace (webhook, Nextcloud Talk)
- [ ] VLAN správa (aplikace migrace), správa SSID/CAPsMAN, PtP, firewall

Mapování funkcí UniFi: [docs/unifi-parity.md](docs/unifi-parity.md).

## Struktura

```
src/mtcontroller/
  config.py        konfigurace (config.toml + MTC_* proměnné)
  db.py            psycopg, tenant_scope / operator_scope pro RLS
  migrate.py       migrátor; SQL schéma v sql/NNNN_*.up.sql
  services.py      tenanti, adopce, schválení, check-in, fronta příkazů
  auth.py, totp.py přihlášení, MFA, relace, role
  routeros.py      generování RouterOS skriptů (adopce, provisioning, příkazy)
  rosapi.py        REST klient RouterOS s pinningem certifikátu
  concentrator.py  konfigurace koncentrátoru
  security.py      hesla, tokeny, šifrování tajemství
  ipam.py          tunelové adresy
  tunnel/          abstrakce koncentrátoru (MikroTik, později RADIUS)
  web/             Starlette: admin UI + API, check-in (oddělené podle hostname)
  worker/          offline detekce, ověření adopce, dorovnání koncentrátorů
  cli.py           mtc-admin
deploy/            install.sh, systemd unity, nginx
tests/             pytest (i end-to-end proti Postgresu) + tests/db (kontroly schématu)
```

## Vývoj

```sh
apt install python3-starlette python3-uvicorn python3-psycopg python3-pydantic \
            python3-cryptography python3-pytest postgresql
# testy s DB potřebují trust autentizaci na unix socketu (jen vývojový stroj!)
sudo -u postgres tests/db/run.sh   # kontroly schématu (RLS, audit, tokeny)
python3 -m pytest                  # unit + end-to-end testy (bez DB se přeskočí)
```
