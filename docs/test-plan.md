# Testovací nasazení – krok za krokem

Cíl prvního testu: čistý Debian 13 jako controller, jeden MikroTik jako
koncentrátor (druhý volitelně jako záložní koncentrátor) a dva testovací
MikroTiky, které se adoptují, dostanou tunel a controller na ně dosáhne přes REST.

Doporučuju začít v **laboratorním režimu** (`--lab`): self-signed certifikáty
a vypnuté ověřování certifikátů na MikroTicích. Odpadá řešení DNS a Let's
Encrypt a otestuje se celý tok. Produkční režim je v sekci 9.

## 0. Co je potřeba

| Prvek | Požadavek |
| --- | --- |
| Controller | Debian 13, IP v admin síti (např. `10.10.0.10`), přístup na internet pro apt |
| Koncentrátor `vpn1` | MikroTik s RouterOS v7, port 443 dostupný z testovacích MikroTiků, IP dostupná z controlleru (např. `10.10.0.1`) |
| Testovací MikroTiky | RouterOS v7 (ideálně aktuální stable), konektivita na controller (443) a koncentrátor (443) |
| Jména | `mtc-admin.lab`, `mtc-checkin.lab`, `vpn1.lab` – v laboratoři stačí statické DNS záznamy |

Rozvržení adres (příklad):

```
controller   10.10.0.10      (admin síť 10.10.0.0/24)
vpn1         10.10.0.1       SSTP na 443, REST pro controller na 8443
tunely       10.200.0.0/16   10.200.0.1 = koncentrátor, tenant „Lab" dostane 10.200.1.0/24
```

## 1. Controller (Debian)

```sh
apt install -y git
git clone https://github.com/UJBenda/Mikrotik-UI /root/Mikrotik-UI
cd /root/Mikrotik-UI
./deploy/install.sh --lab \
  --admin-host mtc-admin.lab --checkin-host mtc-checkin.lab \
  --controller-ip 10.10.0.10 --admin-net 10.10.0.0/24
```

Doplň koncentrátor v `/etc/mtcontroller/config.toml`:

```toml
[[tunnel.concentrators]]
name = "vpn1"
connect_host = "vpn1.lab"
api_url = "https://10.10.0.1:8443"
api_user = "mtc-controller"
api_password_file = "/etc/mtcontroller/vpn1.password"   # vytvořil install.sh
api_fingerprint = "0000000000000000000000000000000000000000000000000000000000000000"  # doplníš v kroku 2
```

Routa na tunelový pool přes koncentrátor (trvale přidej do `/etc/network/interfaces`
nebo do konfigurace systemd-networkd):

```sh
ip route add 10.200.0.0/16 via 10.10.0.1
```

Jména pro controller samotný (kvůli `curl` testům):

```sh
echo "127.0.0.1 mtc-admin.lab mtc-checkin.lab" >> /etc/hosts
systemctl restart mtc-web mtc-worker
curl -sk https://mtc-checkin.lab/healthz      # {"status": "ok", ...}  (díky /etc/hosts jde SNI správně)
```

## 2. Koncentrátor (MikroTik vpn1)

V laboratoři self-signed certifikát pro SSTP (v produkci Let's Encrypt, viz sekce 9):

```
/certificate add name=mtc-sstp common-name=vpn1.lab key-size=prime256v1 days-valid=825 key-usage=tls-server
/certificate sign mtc-sstp
```

Na controlleru vygeneruj konfigurační skript a vlož ho do terminálu koncentrátoru
(nebo ho nahraj jako soubor a dej `/import file-name=vpn1.rsc`):

```sh
mtc-admin concentrator-script vpn1 > /root/vpn1.rsc
```

Skript nastaví PPP profil, SSTP server na 443, REST API na 8443 (jen z IP
controlleru), účet `mtc-controller`, firewall (tunely se nevidí, do tunelů
smí jen controller) a src-nat: správa z controlleru přichází na zařízení
z adresy `10.200.0.1`, takže zařízení nepotřebuje žádnou routu navíc. Na konci vypíše **otisk certifikátu `mtc-api`** – vlož ho
do `api_fingerprint` v config.toml a:

```sh
systemctl restart mtc-web mtc-worker
mtc-admin concentrator-check        # vpn1: OK (...), aktivních SSTP relací: 0
```

Když `concentrator-check` hlásí „otisk certifikátu nesouhlasí", vypíše i skutečný
otisk serveru. Porovnej ho s `/certificate print detail where name=mtc-api`
a teprve pak ho dej do konfigurace.

## 3. Admin účet a UI

```sh
mtc-admin create-superadmin tvuj@email.cz
```

Na počítači v admin síti dej do hosts `10.10.0.10 mtc-admin.lab` a otevři
`https://mtc-admin.lab/` (self-signed – potvrď výjimku v prohlížeči).
Po přihlášení naskenuj klíč do autentizační aplikace a zadej kód.

V UI založ tenanta **Lab** (dostane blok `10.200.1.0/24`) a lokalitu **Stůl**.
(Totéž jde z CLI: `mtc-admin create-tenant Lab`, `mtc-admin create-site Lab Stůl`.)

## 4. Testovací MikroTik – adopce

Na testovacím MikroTiku zajisti překlad jmen (v laboratoři):

```
/ip dns static add name=mtc-checkin.lab address=10.10.0.10
/ip dns static add name=vpn1.lab address=10.10.0.1
```

V UI v kartě **Adopce zařízení** vyber lokalitu, klikni na **Vygenerovat příkaz**
a vlož řádek do terminálu MikroTiku. Sleduj log:

```
/log print where message~"mtc"
```

Očekávaný výsledek: `mtc: zarizeni zaregistrovano, ceka na schvaleni v controlleru`,
v `/system script` a `/system scheduler` je `mtc-checkin`. V UI se zařízení objeví
jako **pending** (do minuty, tabulka se obnovuje sama).

## 5. Schválení a provisioning

V UI zvol lokalitu a roli a klikni **Schválit**. Při dalším check-inu (do 1 min)
zařízení provede provisioning. Kontroly na MikroTiku:

```
/log print where message~"mtc"            # provisioning zacina / hotov
/interface sstp-client print              # mtc-sstp, running
/ip address print where interface=mtc-sstp  # 10.200.1.x
/user print where name=mtc-ctl            # skupina mtc, address 10.200.0.1/32
/ip firewall filter print where comment~"mtc"
```

Na koncentrátoru: `/ppp active print` ukáže `mtc-<sériové číslo>`.
Do 30 s po naběhnutí tunelu worker ověří REST přes tunel a zařízení přejde
do stavu **adopted** (v UI zeleně, vyplní se identita).

Stejně adoptuj druhý testovací MikroTik.

## 6. Bezpečnostní kontroly

| Test | Jak | Očekávání |
| --- | --- | --- |
| Tunely se nevidí | z MikroTiku 1: `/ping 10.200.1.2` (adresa MikroTiku 2) | neodpovídá |
| Z tunelu na koncentrátor nic | z MikroTiku 1: `/ping 10.200.0.1` | neodpovídá |
| Z tunelu na controller nic | z MikroTiku 1: `/tool fetch url=https://10.10.0.10/ check-certificate=no` | timeout |
| Controller → zařízení jen REST/ping | z controlleru: `ping 10.200.1.1`, `curl -k https://10.200.1.1/rest/system/identity` | ping OK, REST vrací 401 bez hesla |
| SSH na zařízení přes tunel | z controlleru `ssh admin@10.200.1.1` | odmítnuto (firewall) |
| Servisní účet nesmí SSH/Winbox | skupina `mtc` | jen api/rest-api politiky |
| Admin UI mimo admin síť | z jiné sítě `https://mtc-admin.lab/` | 403 (nginx) |
| Check-in host neukazuje UI | `curl -k https://mtc-checkin.lab/` | 404 |
| Neplatný klíč | `curl -k -X POST https://mtc-checkin.lab/api/checkin` | 401 |
| Použitý adopční token | vlož stejný příkaz podruhé | fetch selže (404) |
| Zámek účtu | 5× špatné heslo | další pokusy odmítnuty 15 min |
| Audit | UI → Audit | registrace, schválení, adopce, přihlášení |

## 7. Příkazy

- **Restart**: v UI tlačítko Restart → do minuty se zařízení restartuje, po
  startu se samo ozve a tunel naběhne.
- **Znovu provisioning**: idempotentní, vygeneruje se znovu tunel i účet.
- **Odebrat**: zařízení smaže vše, co vytvořil controller (tunel, účet, firewall
  pravidla `mtc:`, check-in skript); v UI zmizí, worker zakáže účet na koncentrátoru.

## 7b. Nové funkce F1 (po adopci)

Všechno níže je jen čtení, kromě NTP plánu, který se aplikuje až po schválení.

| Funkce | Kde | Očekávání |
| --- | --- | --- |
| Inventura | klik na zařízení v UI | do 5 min po adopci: přehled, rozhraní, sousedé, NTP; chyba se ukáže nahoře |
| Analýza VLAN | detail zařízení | stav „odpovídá modelu" / „bez VLAN" / „starý způsob"; u starého způsobu tlačítko Náhled migrace (jen náhled!) |
| Záloha | detail → Zálohovat teď | do minuty řádek v Zálohách; „Rozdíl" ukáže diff se skrytými hesly |
| Drift | změň ručně identitu MikroTiku, Zálohovat teď | v auditu `device.config_changed` |
| Topologie | Lokalita → Topologie | spravovaná zařízení a sousedé (MNDP/LLDP) s porty |
| Aktualizace | seznam zařízení | do 24 h (nebo po restartu workeru) se u verze ukáže ↑ dostupná verze |
| NTP | Lokalita → NTP politika → Uložit → Vygenerovat plány | v Plánech změn plán pro každé zařízení; Schválit odmítne, dokud na zařízení nepovolíš správu NTP (detail → Moduly) |

Záloha přes REST (`/file/read`) je zatím ověřená jen proti simulaci. Když selže,
v `journalctl -u mtc-worker` bude „záloha … selhala" s důvodem – pošli mi ho.

## 8. Záložní koncentrátor (volitelné, druhý MikroTik)

1. Druhý blok `[[tunnel.concentrators]]` s `name = "vpn2"` v config.toml, krok 2 pro vpn2.
2. Nová zařízení dostanou účet na obou koncentrátorech; stávající dorovná worker do 10 min.
3. Test: na vpn1 vypni SSTP server (`/interface sstp-server server set enabled=no`).
   Po 5 check-inech s neběžícím tunelem pošle controller zařízení příkaz k přepnutí
   na vpn2 (v auditu `device.concentrator_failover`).
4. **Omezení**: controller routuje pool `10.200.0.0/16` přes jeden koncentrátor.
   Po přepnutí na vpn2 je potřeba změnit routu (`ip route replace 10.200.0.0/16 via <vpn2>`).
   Automatické směrování přes oba (OSPF z koncentrátorů / per-zařízení /32) je další krok.

## 9. Produkční režim (místo --lab)

- Veřejné DNS pro `mtc-checkin.<doména>` (a `mtc-admin.<doména>`), certifikát Let's Encrypt:
  `./deploy/install.sh --certbot admin@domena.cz ...`
- V config.toml `device_check_certificate = "yes-without-crl"` (výchozí).
- Koncentrátor: `/certificate enable-ssl-certificate dns-name=vpn1.<doména>` (potřebuje
  dočasně port 80 z internetu), pak znovu `concentrator-script`.
- MikroTiky musí důvěřovat kořenové CA Let's Encrypt. **Ověř na své verzi RouterOS**:
  novější v7 mají vestavěné kořenové certifikáty (`/certificate settings`), jinak
  importuj ISRG Root X1. Bez toho adopční `fetch` skončí chybou certifikátu.
- Firewall controlleru (nftables): z internetu jen 443 (a 80 pro ACME), SSH a admin UI jen z admin sítě.
- Zálohuj offline `/etc/mtcontroller/master.key` a databázi (`pg_dump mtcontroller`).

## 10. Řešení problémů

| Příznak | Kde hledat |
| --- | --- |
| Fetch při adopci hlásí chybu certifikátu | sekce 9 (důvěra v CA) nebo laboratorní režim |
| Fetch hlásí 404 | token prošlý (60 min) nebo už použitý – vygeneruj nový |
| Registrace vrátí 400 „spravovane" | zařízení je už v jiném tenantovi nebo adoptované – nejdřív Odebrat |
| Zařízení zůstává **provisioning** | `/log print where message~"mtc"` na MikroTiku; `journalctl -u mtc-worker -f` na controlleru |
| `mtc-sstp` neběží | `/ppp active print` a `/log print where topics~"sstp"` na koncentrátoru; jméno `connect-to` musí sedět s certifikátem (mimo lab režim) |
| Worker hlásí „otisk certifikátu nesouhlasí" u zařízení | porovnej `/certificate print detail where name=mtc-rest` se sloupcem `api_cert_fingerprint` v DB |
| REST přes tunel nejde | `ping 10.200.1.x` z controlleru; routa na pool; `/ip service print` (www-ssl zapnuté, adresa controlleru povolená) |
| Skript selže na `rest-api` politice | starší RouterOS – skript automaticky použije `web`; aktualizuj RouterOS |

Logy: `journalctl -u mtc-web -u mtc-worker -f`, audit v UI nebo
`sudo -u postgres psql mtcontroller -c "select at, action, target from audit_log order by id desc limit 30"`.
