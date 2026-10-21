# Instalace na Debian 13

## Balíček .deb a webový průvodce (doporučeno)

Balíček je `Architecture: all` (čistý Python) – jeden soubor pro amd64, arm64 i armhf.

| Systém | Stav |
| --- | --- |
| Debian 13 (trixie), amd64 / arm64 | podporováno – cílová platforma |
| Raspberry Pi OS na Debianu 13, arm64 | podporováno (Raspberry Pi 4/5) |
| Ubuntu 24.04 LTS | **nejde** – má jen pydantic 1.10 (aplikace potřebuje 2.x); apt instalaci odmítne |
| Debian 12 (bookworm) | **nejde** – pydantic 1.10, Python 3.11 |
| novější Ubuntu (26.04 LTS…) | mělo by jít, pokud má `python3-pydantic` ≥ 2 – zatím neověřeno |

Závislosti v balíčku hlídají minimální verze (`python3 >= 3.12`, `python3-pydantic >= 2`,
`python3-psycopg >= 3`), takže na nepodporovaném systému apt rovnou řekne, co chybí.

```sh
packaging/build-deb.sh            # → dist/mtcontroller_<verze>_all.deb (na libovolném stroji s dpkg-deb)
sudo apt install ./mtcontroller_<verze>_all.deb
```

Instalace doinstaluje závislosti z Debianu, založí databázi, master klíč a pustí migrace.
Na konci vypíše adresu `https://<IP serveru>/` a **kód průvodce** (znovu: `sudo mtc-admin setup-code`).
V prohlížeči se zvolí režim:

- **Lokální controller** – Raspberry Pi / mini PC / VM ve stejné síti jako MikroTiky. Stačí IP adresa,
  bez DNS a bez koncentrátoru. Certifikát vydá vlastní CA controlleru; zařízení si ji při adopci
  stáhnou z `http://<adresa>/mtc-ca.crt` a ponechají jen při shodě otisku vloženého do příkazu.
- **Centrální vzdálená správa** – dvě DNS jména (admin, check-in), Let's Encrypt nebo vlastní CA;
  koncentrátory se doplní do `/etc/mtcontroller/config.toml` jako dřív.

Průvodce se zeptá i na zemi (regulace Wi-Fi), časovou zónu a NTP, založí superadmina (MFA při prvním
přihlášení), první firmu a lokalitu, zapíše konfiguraci, nginx, zapne služby `mtc-web`, `mtc-worker`,
`mtc-syslog` a denní zálohy a udělá první zálohu. Na konci ukáže **záchranné údaje** – hlavně heslo
záloh; ulož si je (stáhnout / vytisknout). Databáze žádná hesla nemá (jen lokální peer přihlášení).
Místo nové instalace jde zvolit **Obnovit ze zálohy** (soubor `.mtcbak` + heslo záloh).

Zálohy: `/var/backups/mtcontroller`, denně, 14 posledních; ručně `sudo mtc-admin backup` nebo v UI
Systém → Zálohy. Zkopíruj je i mimo server. Heslo záloh znovu: `sudo mtc-admin recovery-kit`. Webová část průvodce běží
neprivilegovaně, nastavení systému provádí root služba `mtc-setup-apply` a požadavek ověřuje znovu.

Aktualizace: `sudo apt install ./mtcontroller_<novější>_all.deb` (migrace a restart služeb).

## Skript deploy/install.sh

Instalace i aktualizace z gitu se dělá skriptem `deploy/install.sh`. Používá jen
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
| Balíčky | `python3-starlette`, `python3-uvicorn`, `python3-psycopg`, `python3-pydantic`, `python3-cryptography`, `postgresql`, `nginx`, `ieee-data` (výrobci podle MAC) |
| Uživatel | systémový `mtcontroller`, bez shellu |
| Aplikace | `/opt/mtcontroller/app` (root-owned, jen ke čtení), wrapper `/usr/local/sbin/mtc-admin` |
| Konfigurace | `/etc/mtcontroller/config.toml`, `master.key`, `vpn1.password`, `vpn2.password` (0640 root:mtcontroller) |
| Databáze | role `mtc_owner` (vlastník schématu, jen migrace jako root) a `mtc_app` (aplikace, `NOBYPASSRLS`); peer autentizace přes unix socket, žádná hesla po síti |
| Migrace | `mtc-admin migrate` |
| Služby | `mtc-web`, `mtc-worker` se systemd sandboxem |
| nginx | admin hostname jen z admin sítě, check-in hostname jen `/api/checkin`, `/adopt/`, `/enroll/`, `/healthz`; neznámý hostname = TLS odmítnut |

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
