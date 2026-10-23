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

Certifikáty HTTPS nejsou na Let's Encryptu závislé: průvodce (i skript) vždy nejdřív vydá certifikát
z vlastní CA controlleru, takže nginx naběhne i bez DNS a internetu (prohlížeč ho jen označí za
nedůvěryhodný). Let's Encrypt se zkusí navíc; když selže, instalace doběhne s varováním. Později se
dá kdykoli nastavit v UI **Systém → Certifikát HTTPS** (Let's Encrypt, nahrání vlastního PEM, nové
vydání z CA controlleru) nebo `sudo mtc-admin tls …`:

```sh
mtc-admin tls                                  # stav: druh, jména, platnost
mtc-admin tls letsencrypt it@firma.cz --slot admin
mtc-admin tls custom cert.pem key.pem --slot admin
mtc-admin tls own                              # zpět na CA controlleru
```

nginx ukazuje na `/etc/ssl/mtc/{admin,checkin}.{crt,key}`; když nový certifikát neprojde `nginx -t`,
vrátí se předchozí. Obnovy Let's Encrypt přenáší hook `/etc/letsencrypt/renewal-hooks/deploy/mtcontroller`.
Zařízení ověřují check-in: CA controlleru znají, veřejnou CA jen RouterOS 7.19+ s
vestavěnými kořenovými CA povolenými pro fetch (`/certificate/settings builtin-trust-store`) – proto u vzdálené správy stačí Let's Encrypt
pro administraci a check-in nechat na CA controlleru.

Průvodce se zeptá i na zemi (regulace Wi-Fi), časovou zónu a NTP, založí superadmina (MFA při prvním
přihlášení), první firmu a lokalitu, zapíše konfiguraci, nginx, zapne služby `mtc-web`, `mtc-worker`,
`mtc-syslog` a denní zálohy a udělá první zálohu. Na konci ukáže **záchranné údaje** – hlavně heslo
záloh; ulož si je (stáhnout / vytisknout). Databáze žádná hesla nemá (jen lokální peer přihlášení).
Místo nové instalace jde zvolit **Obnovit ze zálohy** (soubor `.mtcbak` + heslo záloh).

Zálohy: `/var/backups/mtcontroller`, výchozí plán denně ve 2:30 a 14 posledních; plán (čas, dny,
počet) a kopie mimo server se nastavují v UI **Systém → Zálohy**; ručně `sudo mtc-admin backup`.
Heslo záloh znovu: `sudo mtc-admin recovery-kit`. Webová část průvodce běží neprivilegovaně,
nastavení systému provádí root služba `mtc-setup-apply` a požadavek ověřuje znovu.

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

Bez `--certbot` (nebo když Let's Encrypt selže) poběží nginx s certifikátem z CA controlleru
a Let's Encrypt se nastaví později (`mtc-admin tls letsencrypt EMAIL` nebo v UI). `--lab` navíc
vypne ověřování certifikátů na MikroTicích – jen laboratoř, viz [test-plan.md](test-plan.md).

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

Šifrovaná záloha (`.mtcbak`) obsahuje databázi, konfiguraci, `master.key`, CA controlleru
i certifikáty nginx. Plán a cíle: UI **Systém → Zálohy**. Bez hesla záloh (záchranné údaje,
`sudo mtc-admin recovery-kit`) ji nikdo neobnoví – ani ty.

### Kopie mimo server

Controller po každé záloze zkopíruje soubor do všech zapnutých cílů a promaže tam staré (každý
cíl má vlastní „nechat posledních“). Cloudu není potřeba věřit – záloha je šifrovaná už na serveru.
Každý controller potřebuje vlastní složku (retence maže nejstarší zálohy ve složce).

**Nextcloud** – v Nextcloudu Nastavení → Zabezpečení → *Vytvořit nové heslo aplikace*; do cíle
adresu (`https://cloud.firma.cz`), uživatele, heslo aplikace a složku. Tlačítko *Ověřit* zkusí
přihlášení a založí složku.

**Google Drive** – jednou si vytvoř vlastní OAuth klienta (Google sdílené ID pro všechny instalace nedovolí):

1. [Google Cloud Console](https://console.cloud.google.com/) → nový projekt → *API a služby →
   Knihovna* → zapni **Google Drive API**.
2. *OAuth consent screen*: typ External (nebo Internal ve Workspace), název aplikace, e-mail;
   scope `…/auth/drive.file`. **Publish app → In production** – ve stavu Testing vyprší
   přihlášení po 7 dnech. Scope `drive.file` ověření Googlem nepotřebuje.
3. *Credentials → Create credentials → OAuth client ID* → typ **TVs and Limited Input devices**.
4. Client ID a Client secret vlož do cíle v administraci, *Uložit*; v tabulce se ukáže kód –
   zadej ho na <https://www.google.com/device> pod účtem, kam se má zálohovat.

Controller vidí jen soubory, které sám vytvořil (složka *MikroTik-Controller*), ne celý disk.

**OneDrive** (osobní i firemní):

1. [Microsoft Entra](https://entra.microsoft.com/) → *Registrace aplikací → Nová registrace*:
   *Účty v libovolném adresáři organizace a osobní účty Microsoft*; přesměrování nevyplňovat.
2. *Ověřování → Povolit toky veřejného klienta*: **Ano**.
3. *Oprávnění rozhraní API → Microsoft Graph → Delegovaná*: `Files.ReadWrite.AppFolder`
   (a `offline_access`).
4. *Application (client) ID* vlož do cíle (tenant `common`; jen firemní účty: `organizations`
   nebo ID tenanta), *Uložit* a kód zadej na <https://microsoft.com/devicelogin>.

Zálohy budou ve složce *Aplikace/<název aplikace>*; controller jinam nevidí.

Stav cílů ukazuje Systém → Zálohy a `mtc-admin doctor`; když záloha chybí déle, než plán dovoluje,
nebo se kopie dvakrát nepovede, přijde notifikace cílům `[[notify]]` bez omezení na tenanty.

### Obnova na čistém serveru

Nainstaluj balíček, v průvodci zvol **Obnovit ze zálohy**, nahraj `.mtcbak` (ze serveru, z UI nebo
z cloudu) a zadej heslo záloh. Obnoví se data, uživatelé, cíle záloh, CA controlleru i vlastní
certifikát nginx; starší záloha se domigruje. Starý server vypni – oba by zapisovaly do stejné
složky v cloudu. Z příkazové řádky: `sudo mtc-admin restore SOUBOR`.

## Užitečné příkazy

```sh
mtc-admin devices Lab                 # přehled zařízení tenanta
mtc-admin device-check Lab SERIAL     # REST přes tunel: identita, otisk, porty, aktualizace
mtc-admin token Lab "Stůl"            # adopční příkaz z CLI
journalctl -u mtc-web -u mtc-worker -f
systemd-analyze security mtc-web
```
