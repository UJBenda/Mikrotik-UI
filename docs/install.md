# Instalace na Debian 13

Instalace i aktualizace se dělá skriptem `deploy/install.sh`. Používá jen
balíčky z Debianu (žádné PyPI) a dá se spouštět opakovaně.

```sh
git clone https://github.com/UJBenda/Mikrotik-UI /root/Mikrotik-UI
cd /root/Mikrotik-UI
./deploy/install.sh --admin-host mtc-admin.firma.cz --checkin-host mtc-checkin.firma.cz \
  --controller-ip 10.10.0.10 --admin-net 10.10.0.0/24 --certbot admin@firma.cz
```

Pro první test v laboratoři místo `--certbot` použij `--lab` – viz
[test-plan.md](test-plan.md).

Aktualizace: `git pull && ./deploy/install.sh` (konfigurace, klíče i DB zůstanou,
aplikují se nové migrace, služby se restartují).

## Co skript udělá

| Krok | Výsledek |
| --- | --- |
| Balíčky | `python3-starlette`, `python3-uvicorn`, `python3-psycopg`, `python3-pydantic`, `python3-cryptography`, `postgresql`, `nginx` |
| Uživatel | systémový `mtcontroller`, bez shellu |
| Aplikace | `/opt/mtcontroller/app` (root-owned, jen ke čtení), wrapper `/usr/local/sbin/mtc-admin` |
| Konfigurace | `/etc/mtcontroller/config.toml`, `master.key`, `vpn1.password`, `vpn2.password` (0640 root:mtcontroller) |
| Databáze | role `mtc_owner` (vlastník schématu, jen migrace jako root) a `mtc_app` (aplikace, `NOBYPASSRLS`); peer autentizace přes unix socket, žádná hesla po síti |
| Migrace | `mtc-admin migrate` |
| Služby | `mtc-web`, `mtc-worker` se systemd sandboxem |
| nginx | admin hostname jen z admin sítě, check-in hostname jen `/api/checkin`, `/adopt/`, `/healthz`; neznámý hostname = TLS odmítnut |

## Po instalaci

1. Doplň `[[tunnel.concentrators]]` v config.toml a `systemctl restart mtc-web mtc-worker`.
2. `mtc-admin create-superadmin admin@firma.cz`
3. `mtc-admin concentrator-script vpn1` → vložit do koncentrátoru, otisk do config.toml.
4. `mtc-admin concentrator-check`, pak `mtc-admin doctor` (kontrola celé instalace)
5. Routa na tunelový pool přes koncentrátor.
6. Firewall hostu: z internetu jen 443 (a 80 pro obnovu certifikátu), SSH jen z admin sítě.

## Dva koncentrátory

V `[tunnel]` nastav `manage_routes = true` a spusť znovu `./deploy/install.sh`.
Skript přidá drop-in `/etc/systemd/system/mtc-worker.service.d/routes.conf`
(CAP_NET_ADMIN a netlink – nic jiného) a worker udržuje routy `/32` na zařízení
přes koncentrátor, ke kterému jsou připojená (`ip route show proto 186`).
Brána je adresa z `api_url`, jinak `route_via` u koncentrátoru.

## Zálohy

- `/etc/mtcontroller/master.key` – **offline** (bez něj nejdou dešifrovat hesla zařízení).
- Databáze: `sudo -u postgres pg_dump -Fc mtcontroller > mtc-$(date +%F).dump`.

## Užitečné příkazy

```sh
mtc-admin devices Lab                 # přehled zařízení tenanta
mtc-admin device-check Lab SERIAL     # REST přes tunel: identita, otisk, porty, aktualizace
mtc-admin token Lab "Stůl"            # adopční příkaz z CLI
journalctl -u mtc-web -u mtc-worker -f
systemd-analyze security mtc-web
```
