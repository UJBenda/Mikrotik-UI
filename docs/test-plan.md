# Testovací nasazení – krok za krokem

Cíl prvního testu: čistý Debian 13 jako controller, jeden MikroTik jako
koncentrátor (druhý volitelně jako záložní koncentrátor) a dva testovací
MikroTiky, které se adoptují, dostanou tunel a controller na ně dosáhne přes REST.

Doporučuju začít v **laboratorním režimu** (`--lab`): certifikáty z CA controlleru
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

V laboratoři certifikát pro SSTP z lokální CA (v produkci Let's Encrypt, viz sekce 9).
- RouterOS v7 certifikát s `key-usage=tls-server` sám sebou nepodepíše („failure: CA not
  found“), proto nejdřív CA.
- **SSTP potřebuje RSA klíč** – server nabízí jen RSA šifry, s ECDSA (`prime256v1`) klient
  hlásí „no common ciphers“. Skript koncentrátoru ECDSA certifikát odmítne.
- `common-name` musí být přesně `connect_host` z config.toml.

```
/certificate add name=mtc-sstp-ca common-name=mtc-sstp-ca key-size=2048 days-valid=3650 key-usage=key-cert-sign,crl-sign
/certificate sign mtc-sstp-ca
/certificate add name=mtc-sstp common-name=vpn1.lab subject-alt-name=DNS:vpn1.lab key-size=2048 days-valid=825 key-usage=digital-signature,key-encipherment,tls-server
/certificate sign mtc-sstp ca=mtc-sstp-ca
/certificate print detail where name~"mtc-sstp"   # příznak K (privátní klíč), key-type=rsa
```

Na controlleru vygeneruj konfigurační skript a vlož ho do terminálu koncentrátoru
(nebo ho nahraj jako soubor a dej `/import file-name=vpn1.rsc`):

```sh
mtc-admin concentrator-script vpn1 > /root/vpn1.rsc
```

**Pozor:** skript přesune `www-ssl` (WebFig přes HTTPS) koncentrátoru na 8443 a povolí
ho jen z IP controlleru – na 443 poběží SSTP. WebFig přes HTTP, Winbox a SSH se nemění.

Skript nastaví PPP profil, SSTP server na 443, REST API na 8443 (jen z IP
controlleru), účet `mtc-controller`, firewall (tunely se nevidí, do tunelů
smí jen controller) a src-nat: správa z controlleru přichází na zařízení
z adresy `10.200.0.1`, takže zařízení nepotřebuje žádnou routu navíc. Na konci vypíše **otisk certifikátu `mtc-api`** – vlož ho
do `api_fingerprint` v config.toml a:

```sh
systemctl restart mtc-web mtc-worker
mtc-admin concentrator-check        # vpn1: OK (...), aktivních SSTP relací: 0
mtc-admin doctor                    # kontrola celé instalace – vše [OK] kromě případných [!!] varování
```

`doctor` kontroluje konfiguraci, master klíč, připojení k DB, migrace, že
`controller_ip` je adresa serveru, routu na pool přes koncentrátor, DNS check-in
hostu, otisky a REST koncentrátorů a soubory tajemství notifikací. U chyby (`[XX]`)
napíše, co udělat. V laboratoři je `[!!] certifikáty zařízení` v pořádku.

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
/ip service print where name=www-ssl      # zapnuté, certifikát mtc-rest; bylo-li dřív vypnuté,
                                          # address=10.200.0.1/32 (WebFig HTTPS se neotevře do světa)
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
| Z tunelu na controller nic | z MikroTiku 1: `/ping 10.10.0.10 interface=mtc-sstp count=3` | neodpovídá (normální cestou mimo tunel controller dosažitelný je – tudy jde check-in) |
| Controller → zařízení jen REST/ping | z controlleru: `ping 10.200.1.1`, `curl -k https://10.200.1.1/rest/system/identity` | ping OK, REST vrací 401 bez hesla |
| SSH na zařízení přes tunel | z controlleru `ssh admin@10.200.1.1` | odmítnuto (firewall) |
| Servisní účet nesmí SSH/Winbox | skupina `mtc` | jen api/rest-api politiky |
| Admin UI mimo admin síť | z jiné sítě `https://mtc-admin.lab/` | 403 (nginx) |
| Check-in host neukazuje UI | `curl -k https://mtc-checkin.lab/` | 404 |
| Neplatný klíč | `curl -k -X POST https://mtc-checkin.lab/api/checkin` | 401 |
| Použitý adopční token | vlož stejný příkaz podruhé | fetch selže (404) |
| Zámek účtu | 5× špatné heslo | další pokusy odmítnuty 15 min |
| Stav systému | karta Systém (jen superadmin) | „worker běží (kolo N, před X s)“, časy a chyby kroků workeru, fronta příkazů, alerty, velikost DB; `mtc-admin doctor` kontroluje totéž |
| Audit | UI → Audit | registrace, schválení, adopce, přihlášení |
| Uživatelé | UI → Uživatelé tenanta → přidat technika | nový účet dostane dočasné heslo (jen jednou), po přihlášení musí nastavit MFA; technik nevidí správu uživatelů; sám sebe nikdo neodebere |
| Změna hesla | tlačítko Heslo nahoře | vyžaduje současné heslo, ostatní relace se odhlásí |

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
| Upgrade RouterOS | seznam → „Aktualizovat → X“ (nebo detail) | do minuty `mtc: aktualizace RouterOS…` v logu MikroTiku, stažení + restart, po startu nová verze v UI; když se verze do 15 min nezmění, alert „upgrade_failed“. Verze s kritickým známým problémem (např. 7.24–7.24.4) controller odmítne |
| Release notes | detail zařízení s dostupnou aktualizací | sekce „Co přinese X“: důležité změny (!) a změny komponent, které zařízení používá (bridge, wifi, sstp…); controller potřebuje odchozí HTTPS na download.mikrotik.com (stahuje jednou denně, při startu workeru hned) |
| Aktualizace lokality | Lokalita → Aktualizace lokality | náhled pořadí (AP/PtP → switche → gateway), Spustit; po jednom zařízení, další až po návratu s novou verzí (kontrola každou minutu); při chybě nebo 30 min bez návratu se zastaví; Zastavit nechá doběhnout jen právě běžící |
| RouterBOOT | detail → „RouterBOOT → X“ (jen když se liší) | `routerboard upgrade` + restart; po startu stejná current/upgrade verze |
| NTP | Lokalita → NTP politika → Uložit → Vygenerovat plány | v Plánech změn plán pro každé zařízení; Schválit odmítne, dokud na zařízení nepovolíš správu NTP (detail → Moduly) |

| Klienti | Lokalita → Klienti | DHCP/ARP/bridge host/wifi klienti; u každého zařízení + port (u wifi AP, SSID, signál), VLAN; port k jinému spravovanému MikroTiku se nepočítá jako poloha klienta |
| Graf provozu | detail → klik na rozhraní | RX/TX v čase; 6 h/24 h z 5min vzorků, 7 dní a déle hodinové průměry + čárkovaně špičky (první body cca 10 min po adopci) |
| Bezpečnost konfigurace | detail → Bezpečnost konfigurace; dlaždice Bezpečnostní nálezy | nálezy s návrhem opravy (příkaz RouterOS): telnet/ftp/http/api, výchozí admin, MAC server na všech rozhraních, SNMP public, SOCKS/proxy…; veřejná IP bez filtru na input a otevřený DNS = kritický alert. Controller nic neopravuje sám |
| Změna portu | detail → klik na port → editor; nejdřív „Povolit správu portů“ | „Použít“: do minuty provedeno (v historii „čeká na potvrzení“), další check-in → „provedeno“; na MikroTiku během okna `/system scheduler print where name=mtc-confirm`, po potvrzení zmizí |
| Profil portu | editor portu → nastav PVID/VLAN/PoE → „Uložit jako profil…“; na jiném portu vyber profil → „Použít“ | profil vyplní formulář, změna jde stejně jako ruční (s návratem); po další inventuře ukazuje editor portu jméno profilu (port mu odpovídá); smazání profilu porty nemění; v auditu `port_profile.create/delete` |
| Historie portů | odpoj kabel z portu, počkej na inventuru (≤ 5 min), zapoj zpět; detail → Historie portů | „linka dole“ a „linka nahoře“ s časem (nebo „linka N× spadla“, když se to stihlo mezi inventurami); změna portu z editoru se objeví jako změna PVID/VLAN/PoE; editor portu ukazuje historii toho portu |
| Firewall (zóny) | detail gateway → Firewall → Matice zón / Pravidla filtru / NAT | zóny = interface listy (u výchozí konfigurace WAN a LAN); LAN→WAN povoleno, WAN→LAN blokováno „kromě dstnat“, WAN→Router blokováno s výjimkami icmp; klik na buňku = výjimky + rozhodující pravidlo; čísla pravidel odpovídají `/ip firewall filter print` (bez dynamických), počty paketů sedí |
| Automatický návrat | na testovacím switchi vypni uplink port (uplink má varování) | zařízení se odpojí, do ~5 min se samo vrátí, v historii „vráceno zařízením“ a alert; `/log print where message~"mtc"` ukáže „zmena nepotvrzena“ |
| Restart PoE | editor portu s PoE → Restart PoE | zařízení na portu se restartuje |
| Porty | detail → Rozhraní | sloupce Linka (rychlost, half-duplex žlutě, SFP RX výkon), VLAN (PVID · U netagované · T tagované), PoE (stav, W); half-duplex na lince → alert |
| Infrastruktura | Lokalita → Infrastruktura | strom WAN (aktivní/záloha z výchozích rout) → gateway → switche → AP, čísla portů na spojích, CPU/RAM, odznak STP ROOT, offline spoj červeně čárkovaně; vlevo zařízení s mini pruhem portů; volitelně nespravovaní sousedé |
| STP | Lokalita → STP; detail → sloupec STP | root bridge lokality, root port a cena u každého bridge; zablokované porty (alternate) = redundantní cesta; varování: STP vypnuté na bridgi se 2+ linkami ke spravovaným zařízením, root bridge mimo spravovaná zařízení, více rootů |
| Historie klientů | Lokalita → Klienti → „i odpojení“; pole Najít klienta nad lokalitami | sloupce Poprvé/Naposledy, štítek „nový“ (< 24 h); odpojení klienti šedě (30 dní); hledání MAC/IP/jména ve všech lokalitách s poslední polohou. Plní se každých 5 min, uchovává 90 dní |
| Wi-Fi | Lokalita → Wi-Fi | rádia všech AP (i PtP), SSID, kanál, počet klientů, průměrný/nejslabší signál; u CAPsMAN controlleru připojené AP; varování (neběžící rádio, slabí klienti, 40 MHz ve 2,4 GHz, stejný pevný kanál) |
| Alerty | karta Alerty nahoře | např. odpoj ethernet testovacího MikroTiku → do ~5 min „offline" (kritický); po připojení se sám uzavře; Potvrdit jen označí, že o něm víš |
| Notifikace | `mtc-admin notify-test talk` | zpráva v konverzaci Talk (viz níže) |

Záloha přes REST (`/file/read`) je zatím ověřená jen proti simulaci. Když selže,
v `journalctl -u mtc-worker` bude „záloha … selhala" s důvodem – pošli mi ho.

Prahy alertů (v `alerts.py`): CPU/paměť ≥ 90 %, ≥ 5 výpadků linky za 2 h
(flapping), ≥ 100 chyb portu za hodinu, záloha starší 48 h, tunel neběží ≥ 3 check-iny,
half-duplex na lince, DHCP pool ≥ 90 % (`health.py`), NTP klient nesynchronizovaný
víc než hodinu po startu, aktualizace se nepovedla.

### Notifikace do Nextcloud Talk

1. Na Nextcloudu (Talk 17.1+ / Nextcloud 27+):
   ```sh
   SECRET=$(openssl rand -hex 32); echo "$SECRET"
   sudo -u www-data php occ talk:bot:install "MikroTik" "$SECRET" "https://mtc.invalid/" "Alerty MikroTik controlleru"
   sudo -u www-data php occ talk:bot:list            # ID bota
   sudo -u www-data php occ talk:bot:setup <ID> <token konverzace>
   ```
   Token konverzace je konec URL konverzace (`…/call/<token>`). URL bota (třetí
   parametr) se pro odesílání nepoužívá – bot jen posílá zprávy.
2. Na controlleru:
   ```sh
   echo "$SECRET" > /etc/mtcontroller/talk.secret
   chown root:mtcontroller /etc/mtcontroller/talk.secret; chmod 0640 /etc/mtcontroller/talk.secret
   ```
   a v config.toml odkomentuj blok `[[notify]]` s `kind = "nextcloud_talk"`,
   `url = "https://<cloud>/ocs/v2.php/apps/spreed/api/v1/bot/<token konverzace>/message"`.
3. `systemctl restart mtc-worker`, pak `mtc-admin notify-test talk`.
   Chyba 401 = nesedí tajemství nebo bot není v konverzaci povolený.

E-mail: blok `[[notify]]` s `kind = "email"` (viz config.toml.example), heslo k SMTP
do souboru v `secret_file` (0640 root:mtcontroller), `mtc-admin notify-test mail`.
Bez TLS (smtps://) nebo STARTTLS (smtp://) se e-mail neodešle.

Neodeslané notifikace worker zkouší znovu (max. 5×); stejně funguje JSON webhook
(`kind = "json"`, podpis v hlavičce `X-MTC-Signature: sha256=<HMAC těla>`).

## 8. Záložní koncentrátor (volitelné, druhý MikroTik)

1. Druhý blok `[[tunnel.concentrators]]` s `name = "vpn2"` v config.toml, krok 2 pro vpn2.
2. Nová zařízení dostanou účet na obou koncentrátorech; stávající dorovná worker do 10 min.
3. Test: na vpn1 vypni SSTP server (`/interface sstp-server server set enabled=no`).
   Po 5 check-inech s neběžícím tunelem pošle controller zařízení příkaz k přepnutí
   na vpn2 (v auditu `device.concentrator_failover`).
4. **Routy na controlleru**: zapni v config.toml `manage_routes = true` (sekce `[tunnel]`)
   a spusť znovu `./deploy/install.sh` (nasadí workeru CAP_NET_ADMIN; jinak nic nemění).
   Worker pak každou minutu udržuje routy `/32` na zařízení přes koncentrátor, ke kterému
   jsou připojená:
   ```sh
   ip route show proto 186        # 10.200.1.5 via 10.10.0.2 …
   mtc-admin doctor               # [OK] routy přes koncentrátory
   ```
   Statickou routu celého poolu přes vpn1 nech – je to záloha, /32 má přednost.
   Bez `manage_routes` po přepnutí na vpn2 nepůjde REST (inventura, zálohy), dokud
   ručně nezměníš routu (`ip route replace 10.200.0.0/16 via <vpn2>`); check-in
   a příkazy fungují i tak.

## 9. Produkční režim (místo --lab)

- Veřejné DNS pro `mtc-checkin.<doména>` (a `mtc-admin.<doména>`), certifikát Let's Encrypt:
  `./deploy/install.sh --certbot admin@domena.cz ...` (když selže, běží CA controlleru a LE se
  doplní později: `mtc-admin tls letsencrypt EMAIL` nebo UI Systém → Certifikát HTTPS).
- V config.toml `device_check_certificate = "yes-without-crl"` (výchozí).
- Koncentrátor: `/certificate enable-ssl-certificate dns-name=vpn1.<doména>` (potřebuje
  dočasně port 80 z internetu), pak znovu `concentrator-script`.
- MikroTiky musí důvěřovat kořenové CA Let's Encrypt. **Ověř na své verzi RouterOS**:
  novější v7 mají vestavěné kořenové certifikáty (`/certificate settings`), jinak
  importuj ISRG Root X1. Bez toho adopční `fetch` skončí chybou certifikátu.
- Firewall controlleru (nftables): z internetu jen 443 (a 80 pro ACME), SSH a admin UI jen z admin sítě.
- Zálohuj offline `/etc/mtcontroller/master.key` a databázi (`pg_dump mtcontroller`).

## 9b. Ověření předpokladů na testovacím MikroTiku (pro další fáze)

Tyhle věci jsem nemohl vyzkoušet bez skutečného RouterOS. Stačí pustit a poslat mi
výstup (nebo jen „OK“ / „nefunguje: …“). Body 1–4 se týkají toho, co už je hotové,
body 5–6 jsou předpoklady pro bezpečné aplikování změn (VLAN, firewall).

1. **REST přes tunel** (pinning otisku, monitor portů, kontrola aktualizací) – jedním příkazem:
   ```sh
   mtc-admin device-check Lab <sériové číslo>
   ```
   Čekáš `[OK]` u identity, verze, ethernet monitoru (rychlosti portů) a aktualizací.
   Při `[XX] otisk certifikátu` porovnej
   `echo | openssl s_client -connect 10.200.1.1:443 2>/dev/null | openssl x509 -noout -fingerprint -sha256`
   s `/certificate print detail where name=mtc-rest` na zařízení a pošli mi oba.
2. **Detail zařízení v UI** – vyplněné sloupce Linka, VLAN, STP (a PoE u PoE portů).
3. **Kontrola aktualizací** – v detailu je „Kanál / dostupná“ a u starší verze tlačítko Aktualizovat.
4. **Release notes** – controller potřebuje odchozí HTTPS na `download.mikrotik.com`:
   `curl -sI https://download.mikrotik.com/routeros/7.20/CHANGELOG | head -1`.
5. **Jednorázový scheduler za N minut** (základ automatického návratu změny):
   ```
   /system scheduler add name=mtc-test start-date=[/system clock get date] start-time=[/system clock get time] interval=2m on-event=":log warning MTC-TEST; /system scheduler remove [find name=mtc-test]"
   ```
   Za ~2 min v `/log print where message~"MTC-TEST"` jeden řádek a scheduler zmizí?
6. **Obnova zálohy ze skriptu** (jen na testovacím kusu – restartuje ho!):
   ```
   /system backup save name=mtc-test password=Test12345
   /system script add name=mtc-restore source="/system backup load name=mtc-test.backup password=Test12345"
   /system script run mtc-restore
   ```
   Restartuje se a obnoví bez potvrzování? (V terminálu se `backup load` ptá – jde o to,
   jestli se ptá i ze skriptu.) Po testu: `/system script remove mtc-restore`, `/file remove mtc-test.backup`.
7. **Loop protect** (smyčka na portu) – na switchi zapni `/interface ethernet set ether5 loop-protect=on`
   a propoj patch kabelem ether5 s jiným portem téhož bridge. Pak
   `/interface ethernet print detail where name=ether5` – čekám `loop-protect-status=disable`
   (port dočasně vypnutý). Controller z toho dělá kritický alert „smyčka“, ↻ na portu a skóre anomálie.
   Pošli mi přesný výpis, ať sedí název a hodnota pole.
8. **Skutečný kanál Wi-Fi** (plán kanálů) – na AP s balíkem `wifi`:
   `/interface wifi monitor [find] once` – čekám pole `channel` ve tvaru `5180/ax/Ceee`
   (frekvence / standard / pozice řídicího kanálu). Pošli výpis, hlavně u rádia s kanálem „auto“.
9. **DHCP snooping** (na testovacích kusech, ne v produkci):
   - Lokalita → Sítě a DHCP: sedí trusted porty s tím, kudy vede cesta k DHCP serveru?
   - Povol modul DHCP snooping na switchi, vytvoř a schval plán. Pak na klientovi za switchem
     obnov adresu (`ipconfig /renew`) – musí ji dostat.
   - Na **routeru s DHCP serverem na VLAN rozhraní téhož bridge** po zapnutí snoopingu: dostávají
     klienti dál adresu? (ověřuje, že bridge/CPU je pro snooping důvěryhodný.)
   - Připoj na access port druhý MikroTik s DHCP serverem: klienti od něj adresu nedostanou a
     `/ip dhcp-server alert print` na routeru ukáže `unknown-server` → v UI kritický alert.
   - `/interface bridge port print` – porty mají dál příznak H (HW offload)?
10. **DHCP rezervace** (Sítě a DHCP → klik na počet leases): povol modul DHCP leases na routeru,
    u dynamického klienta „Vytvořit rezervaci“ s jinou IP a DHCP option (např. 42 NTP).
    Pak `/ip dhcp-server lease print detail where mac-address=…` – statický lease s novou IP,
    `dhcp-option` a komentářem; `/ip dhcp-server option print` – nová option s `comment=mtc`.
    Klient po `ipconfig /release` + `/renew` dostane novou IP. Pošli výpis, kdyby `make-static`
    ze skriptu hlásil chybu.

## 9c. Řízený režim – převzetí switche (F5a)

Jen na testovacím kusu, který smíš resetovat. Ideálně switch (CRS) nebo hEX, kterým jde
uplink k routeru labu; do jiného portu notebook.

1. **Předpoklady RouterOS** (odpovědi mi pošli – podle nich případně upravím bootstrap):
   - bod 6 z 9b (`backup load` ze skriptu bez potvrzování) – na něm stojí návrat;
   - `/system reset-configuration keep-users=yes no-defaults=yes skip-backup=yes run-after-reset=x.rsc`
     se souborem, který obsahuje jen `:log warning MTC-BOOT` – po startu je v logu `MTC-BOOT`?
     A zůstal certifikát `mtc-rest` (`/certificate print`) a uživatel `mtc-ctl` (`/user print`)?
2. **Záměr lokality**: Lokalita → Sítě → Záměr lokality. Sítě: management (VLAN např. 99,
   účel management, podsíť a brána labu), kancelář (VLAN 10), hosté (VLAN 20). „Management bez
   tagu na trunkách“ nech zaškrtnuté (router labu posílá síť bez tagu). Uložit.
3. **Náhled**: detail switche → záložka Řízení. Zkontroluj: uplink je port k routeru, port
   s notebookem je „přístup – Kancelář“, VLAN tabulka, žádná červená překážka. Notebookový port
   můžeš přepnout na management, ať se k switchi dostaneš i po převzetí („přístup – Management“,
   Uložit výjimky).
4. **Převzetí**: Vytvořit plán převzetí → Plány změn → Schválit a aplikovat. Během ~1 min se
   switch zazálohuje a resetuje; v logu na zařízení (Winbox přes MAC) `mtc: bootstrap prevzeti zacina`
   a `mtc: bootstrap hotov`. Do pár minut se ozve přes tunel, plán přejde na **applied** a záložka
   Řízení ukáže „řízené“. Pošli `/export` po převzetí.
5. **Návrat**: na druhém (ještě nepřevzatém) kusu schval převzetí a hned po resetu **odpoj uplink**.
   Za ~12 min se switch sám vrátí ze zálohy (restart do původní konfigurace); po zapojení uplinku se
   ozve a plán bude **rolled_back** (hlásí to revert skript; když hlášení neprojde, pozná to controller
   z prvního check-inu po termínu, který přijde ze staré konfigurace).
6. **Sladění**: na řízené lokalitě přidej v záměru síť (VLAN 30) → Sladit řízená zařízení. Čekám
   „jen přidání VLAN – aplikuje se samo“ a na switchi `/interface bridge vlan print` řádek 30
   tagovaný na trunkách.

## 9d. Řízený režim – Wi-Fi přes CAPsMAN (F5b)

Potřebuješ převzatý switch (bod 9c) a aspoň jedno AP s balíkem `wifi` (cAP ax, hAP ax², cAP ac s wifi-qcom-ac).

1. **CAPsMAN**: Lokalita → Sítě → Záměr → Wi-Fi: „CAPsMAN na“ = převzatý switch → Uložit nastavení Wi-Fi.
2. **SSID**: karta „Nová Wi-Fi síť“ – např. „Firma“ v síti Kancelář a „Hoste“ v síti Hosté s izolací klientů.
3. **AP**: detail AP → Řízení → Role „AP + switch“ → Uložit výjimky → Vytvořit plán převzetí → schválit.
   Po převzetí: `/interface wifi cap print` na AP (enabled, caps-man-names = identita switche).
4. **Nasadit Wi-Fi** → přehled rádií a kanálů → plán → schválit. Na switchi:
   `/interface wifi capsman remote-cap print`, `/interface wifi print` (dynamická rozhraní AP s SSID),
   `/interface wifi steering print` (rrm, wnm – pošli výpis, kdyby příkaz hlásil chybu).
5. **Provoz**: klient na „Firma“ dostane adresu z VLAN 10, na „Hoste“ z VLAN 20 a nevidí ostatní klienty.
   Na AP do minuty: `/system script print where name=mtc-wifi-vlan` (komentář `mtc-wifi:<otisk>`),
   `/interface bridge port print where comment=mtc-wifi` (každé wifi rozhraní s pvid své sítě).
6. **Roaming**: se dvěma AP projdi s telefonem mezi nimi; na switchi
   `/log print where topics~"wireless"` – čekám „roamed“/FT. Pošli výpis, hlavně u WPA3 klientů.

## 9d2. Skupiny AP, více hesel, cAP ac

Potřebuješ CAPsMAN z 9d, jedno cAP ax a jedno cAP ac (wifi-qcom-ac) jako „AP + switch“.

1. **Skupina**: Wi-Fi → Skupiny AP → + Skupina „Sklad“. Přetáhni cAP ax do sloupce Sklad (nebo pravé
   tlačítko na AP → Přesunout do → Sklad). V Auditu `device.ap_group`.
2. **SSID jen pro skupinu**: pravé tlačítko na sloupci Sklad → „Nová Wi-Fi jen pro tuto skupinu“ – „Sklad-WiFi“.
   Nasadit Wi-Fi: v přehledu rádií má Sklad-WiFi jen cAP ax. Na cAP ac `/interface wifi print` – Sklad-WiFi tam není.
3. **cAP ac a VLAN**: na cAP ac `/interface bridge port print where comment=mtc-wifi` – slave rozhraní
   (wifi3/wifi4…) s pvid sítě „Hoste“. Restartuj cAP ac (jména slave rozhraní se můžou prohodit) –
   do 20 s po naběhnutí musí mít porty zase správné pvid. Pošli výpis před a po restartu.
4. **Více hesel**: karta „Firma“ → editor → Více hesel: „Ucetni“ → síť Hosté, heslo; Uložit, Nasadit Wi-Fi.
   Na switchi `/interface wifi security multi-passphrase print` (skupina mtc-mp…, vlan-id). Telefon s heslem
   „Ucetni“ dostane adresu z VLAN Hosté, s hlavním heslem z Kancelář. Na cAP ac se „Firma“ už nevysílá
   (karta ukazuje „na 1 AP bez ax se nevysílá“).
5. **FT s více hesly**: projdi mezi dvěma ax AP s heslem „Ucetni“ – klient zůstane ve VLAN Hosté.
6. **Odhalení**: pravé tlačítko na kartě → Zobrazit hesla; v Auditu `site.ssid_ppsk_reveal`, v DB ani v
   plánu (`config_plan.script`) heslo není (`MTCMP…` token).
7. **Zrušení skupiny**: pravé tlačítko na sloupci → Smazat skupinu – AP se vrátí mezi „Bez skupiny“,
   Sklad-WiFi se vysílá na všech AP (po Nasadit Wi-Fi).

## 9e. Výchozí konfigurace podle role a ohlášení

1. Lokalita → Nasazení: u role **switch** zkopíruj příkaz a vlož ho do terminálu testovacího kusu
   (smaže konfiguraci, uživatelé zůstanou). Po restartu: `/interface bridge print` (komentář
   `mtc-default:switch`), `/system scheduler print` (mtc-enroll).
2. Do minuty se kus objeví v Přehledu → Adopce jako čekající s rolí switch. Schval ho. Po provisioningu
   je v detailu → Řízení „řízené“ (bez převzetí resetem).
3. Netinstall: `netinstall-cli -r -s mtc-default-ap.rsc -a <IP> routeros-…npk wifi-qcom-…npk` na cAP ax.
   Po instalaci a po **resetu tlačítkem** musí naběhnout zase naše výchozí konfigurace (ne tovární).
4. Firewall (hEX / RB2011): role firewall – klient na ether2 dostane 192.168.88.x a internet; z WAN nejde
   Winbox ani ping na router ze sítě, která není v `mtc-admin`. Detail → Bezpečnost a firewall → Matice zón
   ukazuje LAN → WAN povoleno, WAN → LAN zakázáno.
5. Přepnutí managementu: v Řízení nastav statickou adresu → Sladit řízená zařízení → plán schval. Zařízení se
   ozve z nové adresy; s úmyslně špatnou bránou se do 5 minut samo vrátí.
6. Výchozí hesla: ulož heslo ze štítku, „Zobrazit“ ho ukáže na 20 s a v Auditu je záznam `factory_password.reveal`.

## 10. Řešení problémů

| Příznak | Kde hledat |
| --- | --- |
| Fetch při adopci hlásí chybu certifikátu | sekce 9 (důvěra v CA) nebo laboratorní režim |
| Fetch hlásí 404 | token prošlý (60 min) nebo už použitý – vygeneruj nový |
| Registrace vrátí 400 „spravovane" | zařízení je už v jiném tenantovi nebo adoptované – nejdřív Odebrat |
| Zařízení zůstává **provisioning** | `/log print where message~"mtc"` na MikroTiku; `journalctl -u mtc-worker -f` na controlleru |
| SSTP: `ssl: no common ciphers` | certifikát SSTP na koncentrátoru je ECDSA – vystav RSA 2048 (sekce 2) a `/interface sstp-server server set certificate=mtc-sstp` |
| Tunel zařízení je `connected`, ale zařízení zůstává **provisioning** a `ping 10.200.1.x` z controlleru nejde | na koncentrátoru `/ip firewall address-list print where list=mtc-controller` musí být IP Debianu (ne koncentrátoru); `/ip firewall filter print stats where comment~"mtc"` – počítadlo u `drop do tunelu` roste = špatná adresa; na Debianu `ip route get 10.200.1.1` nesmí jít přes výchozí bránu (`redirected`) – `ip route flush cache`, redirecty vypíná install.sh |
| Tlačítka v UI „nic nedělají“ | potvrzení je vlastní dialog ve stránce (na nastavení dialogů prohlížeče nezáleží); chyba skriptu se ukáže vpravo dole jako „Chyba v UI“; jinak F12 → Konzole a `journalctl -u mtc-web -n 50` |
| Zařízení nejde odebrat („Odebrat“ nic neudělá) a znovu adoptovat („zařízení je už spravované“) | „Odebrat“ čeká, až úklid provede samo zařízení; když neodpovídá, použij **Zapomenout** (odebere ho jen z controlleru, klíče zneplatní, účet na koncentrátoru vypne worker). Pak normální adopce; na zařízení lze předem ručně smazat `/interface sstp-client remove [find name=mtc-sstp]`, `/system scheduler remove [find name~"^mtc"]`, `/system script remove [find name~"^mtc"]`, `/ip firewall filter remove [find comment~"^mtc:"]` |
| `mtc-sstp` neběží | `/ppp active print` a `/log print where topics~"sstp"` na koncentrátoru; jméno `connect-to` musí sedět s certifikátem (mimo lab režim) |
| Worker hlásí „otisk certifikátu nesouhlasí" u zařízení | porovnej `/certificate print detail where name=mtc-rest` se sloupcem `api_cert_fingerprint` v DB |
| `nginx -t` hlásí „Address family not supported“ | server má vypnuté IPv6 – v `/etc/nginx/sites-available/mtcontroller.conf` smaž řádky `listen [::]:…` |
| Nic nefunguje a nevíš proč | `mtc-admin doctor` – projde konfiguraci, DB, worker, síť i koncentrátory |
| REST přes tunel nejde | `mtc-admin device-check Lab SERIAL`; `ping 10.200.1.x` z controlleru; routa na pool; `/ip service print` (www-ssl zapnuté, adresa controlleru povolená) |
| Zařízení se hlásí (UI „naposledy“ se mění), ale příkazy se neprovedou | `/log print where message~"mtc"` – „check-in selhal“; `/file print where name~"mtc"`; pošli mi výpis (možná jiné chování `fetch … as-value` na tvé verzi) |
| Provisioning `failed` | `/log print where topics~"script"`, ruční zkouška: `/system script run mtc-checkin` v terminálu ukáže chybu s řádkem |
| Skript selže na `rest-api` politice | starší RouterOS – skript automaticky použije `web`; aktualizuj RouterOS |

Logy: `journalctl -u mtc-web -u mtc-worker -f`, audit v UI nebo
`sudo -u postgres psql mtcontroller -c "select at, action, target from audit_log order by id desc limit 30"`.
