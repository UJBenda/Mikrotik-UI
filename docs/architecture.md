# MikroTik Controller – návrh architektury

Stav k 2026-10-18. Živá verze dokumentu je v Claude Docs; tento soubor je verzovaná kopie u kódu.

## Cíl a přijatá rozhodnutí

Stavíme vlastní webový controller pro MikroTik ve stylu UniFi: víc firem (tenantů), každá s mnoha lokalitami, lokalita klidně se 40 zařízeními (routery, switche, AP, PtP spoje). Cílem je centrální správa, topologická mapa, sledování klientů, VLAN a provozu, statistiky a hlídání chyb.

| Oblast | Rozhodnutí |
| --- | --- |
| Konektivita | Výhradně odchozí spojení ze zařízení na TCP 443 |
| Hlavní kanál | SSTP tunel (skutečné TLS), jeden tunel na každé zařízení |
| Záložní kanál | HTTPS check-in přes `/tool fetch` ze scheduleru |
| Koncentrátor | Fyzický MikroTik; v kódu abstrakce pro pozdější accel-ppp + RADIUS na Linuxu |
| Běh controlleru | Nativně na Debianu, bez Dockeru (systemd, PostgreSQL, nginx) |
| Jazyk | Python (FastAPI, SQLAlchemy/Alembic) |
| Změny konfigurace | Desired state + automatický rollback |
| RouterOS | Pouze v7 (REST API); v6 nepodporujeme |
| Měřítko | Stovky zařízení, s rezervou na víc |
| Web UI | Jen pro administrátory, dostupné z VPN / admin sítě |
| CA | Vlastní CA pro klientské certifikáty, generovaná na serveru, root uložený offline |
| Retence | Měsíce (metriky, IPFIX, audit) kvůli dohledání incidentů |
| Redundance | Druhý koncentrátor se připraví v návrhu od začátku |

## Architektura

Controller je sada systemd služeb na jednom Debianu; koncentrátor je samostatný fyzický MikroTik v DMZ s vlastní veřejnou IP a hostname. Zařízení v terénu nemají otevřený žádný port.

```
Zařízení v terénu                       Koncentrátor (DMZ)            Controller (Debian)
┌──────────────────────┐  SSTP 443     ┌────────────────────┐  API  ┌──────────────────────┐
│ Firma A · lokalita 1 │──────────────►│ MikroTik SSTP      │◄──────│ nginx · TLS          │
│ Firma A · lokalita 2 │               │ tunel↔tunel drop   │       │ mtc-web · UI + API   │
│ Firma B · lokalita 1 │               └────────────────────┘       │ mtc-worker           │
│ (žádný otevřený port)│──────── HTTPS check-in 443 (záložní) ─────►│ mtc-collector        │
└──────────────────────┘                                            │ PostgreSQL+Timescale │
                                                                    └──────────┬───────────┘
                                                                               │ syslog, audit
                                                                        ┌──────▼──────┐
                                                                        │ Wazuh SIEM  │
                                                                        └─────────────┘
```

Zařízení otevírají jen odchozí spojení; správu začíná vždy controller a do tunelů vede jen přes koncentrátor.

| Balíček (.deb) | Služba | Obsah |
| --- | --- | --- |
| `mtc-core` | `mtc-web` | Web UI, REST API, tenanti, uživatelé, adopce, DB migrace |
| `mtc-worker` | `mtc-worker` | Polling, fronta změn, rollback, firmware rollout |
| `mtc-collector` | `mtc-collector` | IPFIX kolektor, příjem syslogu; lze přesunout na jiný stroj |
| `mtc-backup` | součást workeru | Příjem, šifrování a verzování záloh, diffy |
| moduly v aplikaci | — | VLAN, CAPsMAN, PtP, topologie; zapínají se per tenant |

Adresáře:

- `/opt/mtcontroller/` – aplikace + Python venv
- `/etc/mtcontroller/` – `config.toml`, certifikáty, master klíč (root:mtcontroller, 0640)
- `/var/lib/mtcontroller/` – zálohy, exporty, uploady z check-inů

Pravidla pro přenositelnost: konfigurace jen z `config.toml` / env, žádné natvrdo zadané hostitele, data jen ve vyjmenovaných adresářích. Služby běží pod uživatelem `mtcontroller`, ne jako root.

Tunelový pool (např. 10.200.0.0/16) je mimo všechny sítě zákazníků. Na controlleru je jedna statická routa na pool přes koncentrátor.

## Konektivita

Každé zařízení si samo otevře dvě odchozí spojení na 443 a obě musí být skutečné TLS, aby prošla i přes DPI.

1. **SSTP tunel** (`/interface sstp-client`) na hostname koncentrátoru. Platný certifikát a `verify-server-certificate=yes`. Přes tunel controller mluví s REST/API-SSL/SSH zařízení.
2. **HTTPS check-in** (scheduler + `/tool fetch`) na `https://<checkin-host>/api/checkin` každé 1–5 min. Posílá heartbeat a stav tunelu, stahuje frontu příkazů, nahrává zálohy. Funguje i bez tunelu, takže přes něj jde tunel opravit.

Jeden tunel na každé zařízení, ne jen na gateway lokality. Důvod: zákazníci mají překrývající se LAN (192.168.88.0/24 všude) a s vlastní tunelovou IP má každé zařízení unikátní management adresu bez NATu a bez sahání do LAN zákazníka.

Adresní plán poolu: `10.200.0.0/16` rozdělený na /24 bloky přidělované tenantům (254 zařízení na blok, tenant může mít víc bloků). Bloky jsou jen pro přehlednost, izolaci vynucuje firewall. Pro stovky až tisíce zařízení pool stačí; v případě potřeby lze přidat další /16.

Omezení: SSTP je TCP-over-TCP, propustnost je nižší. Pro správu, polling a zálohy to stačí; IPFIX z velkých gatewayí může tunel vytížit (viz Sběr dat). Při stovkách tunelů je potřeba koncentrátor s dostatečným CPU na TLS (RB5009 / CCR2004 a výš).

Redundance: SSTP klient míří na hostname, ne IP. Druhý koncentrátor se stejnou konfigurací PPP secretů (synchronizuje controller) a skript na zařízení, který při výpadku přepne `connect-to` na záložní hostname.

## Bezpečnost

Princip: zařízení se nevidí navzájem, zařízení nevidí controller jinak než přes dvě definované služby a controller je jediné místo, odkud jde zařízení spravovat. Kompromitace jednoho zařízení nebo jednoho tenanta nesmí otevřít cestu dál.

### Koncentrátor (vrstva 1)

- PPP profil: `interface-list=TUNNELS`, `only-one=yes`, `change-tcp-mss=yes`, žádný bridge, /32 point-to-point.
- Forward: povolit jen `src=<controller>` → pool na portech 443 (REST), 8729 (API-SSL), 22 (SSH), 8291 (Winbox); established/related; všechno ostatní z `TUNNELS` drop, včetně tunel↔tunel a tunel→internet.
- Input z `TUNNELS`: drop (koncentrátor se z tunelů nedá spravovat).
- Autentizace: unikátní jméno + dlouhé náhodné heslo per zařízení **a** klientský certifikát z vlastní CA controlleru. Odebrání zařízení = zakázání secretu + revokace certifikátu (CRL).
- Správa koncentrátoru jen z management sítě controlleru, ne z internetu. Rate-limit nových TLS spojení na 443 proti brute-force a DoS.

### Zařízení (vrstva 2)

- `/ip service`: www-ssl, api-ssl, ssh, winbox s `address=<controller>/32`; telnet, ftp, www, api (plaintext) vypnuté.
- Input firewall: management povolený jen ze SSTP rozhraní a jen od IP controlleru; zbytek podle role zařízení.
- Uživatelé: servisní účet controlleru se skupinou s minimálními právy, lokální admin účet zákazníka zůstává, ale je auditovaný.
- Vypnout MAC-server / MAC-Winbox a neighbor discovery na klientských a WAN portech; zapnuté jen na infrastrukturních.
- `/ip ssh strong-crypto=yes`, `/tool bandwidth-server` vypnout, `/ip proxy`, `/ip socks`, `/ip upnp` vypnuté, pokud nejsou potřeba.
- Tyto položky jsou **baseline profil**, který controller pravidelně kontroluje a odchylky hlásí.

### Controller host (vrstva 3)

- Debian minimal, `unattended-upgrades`, nftables default drop. Z internetu jen 443 pro check-in přes nginx; web UI a SSH jen z admin sítě / VPN.
- Postgres poslouchá jen na localhost / unix socketu; služby pod vlastním uživatelem, systemd sandbox (`ProtectSystem=strict`, `NoNewPrivileges`, `PrivateTmp`, `CapabilityBoundingSet=`).
- Web UI a check-in API na **různých hostname** a v nginx na různých `server` blocích; UI má `allow <admin síť>; deny all;`.

### Aplikace a víc firem (vrstva 4)

- Hierarchie tenant → lokalita → zařízení. Každý dotaz filtruje `tenant_id` v aplikaci a Postgres **row-level security** to vynucuje podruhé (`SET app.tenant_id` per transakce, aplikace se připojuje rolí bez `BYPASSRLS`).
- Role: superadmin (provozovatel), tenant-admin, technik, read-only; práva lze omezit na lokality.
- Povinné MFA (TOTP nebo WebAuthn) pro všechny přihlašovací účty; session cookies `Secure`, `HttpOnly`, `SameSite=Strict`; CSRF ochrana.
- Check-in API: každé zařízení má vlastní token (v DB jen hash), požadavek podepsaný HMAC s časovou značkou proti replay; volitelně mTLS stejným klientským certifikátem jako SSTP.
- Příkazy z fronty smí obsahovat jen předem definované akce, ne libovolný skript z webu, pokud uživatel nemá výslovné právo.

### Tajemství

- Hesla zařízení, API tokeny a zálohy `show-sensitive` šifrované v DB/na disku (envelope encryption, master klíč mimo DB v `/etc/mtcontroller/`, později HSM/TPM nebo Vault).
- Pravidelná rotace hesel servisních účtů na zařízeních, řízená controllerem.
- Vlastní CA: root CA vygenerovaná na serveru, poté přesunutá offline (šifrovaný USB / trezor); na serveru zůstává jen intermediate CA, která podepisuje klientské certifikáty zařízení.

### Audit

- Každá akce (kdo, kdy, na čem, diff před/po) do neměnného audit logu v DB (append-only, trigger brání UPDATE/DELETE) a zároveň jako syslog do Wazuhu.
- Alerty: nové přihlášení do zařízení mimo controller, změna konfigurace mimo controller (drift), opakované selhání SSTP autentizace, neznámý klient na management VLAN.

## Adopce zařízení

Adopce je jeden řádek vložený do terminálu MikroTiku; zařízení pak čeká ve stavu „pending“, dokud ho technik v controlleru neschválí a nepřiřadí k lokalitě.

1. Technik v controlleru vygeneruje **jednorázový adopční token** pro tenanta/lokalitu (platnost např. 1 h, jedno použití).
2. Na zařízení: `/tool fetch url="https://<checkin-host>/adopt/<token>" dst-path=adopt.rsc; /import adopt.rsc`.
3. Bootstrap skript pošle identitu zařízení (sériové číslo, model, verze RouterOS, MAC) a dostane zpět jen dočasný check-in klíč.
4. Controller zobrazí zařízení jako pending; technik ověří sériové číslo a schválí.
5. Po schválení controller přes check-in doručí konfiguraci: klientský certifikát + klíč, SSTP klienta (hostname, uživatel, heslo, `verify-server-certificate=yes`), servisní účet API, omezení `/ip service`, input firewall pro management, scheduler check-inu. Na koncentrátoru založí `/ppp/secret` s pevnou IP.
6. Controller ověří, že tunel naběhl a API odpovídá; teprve pak smaže dočasný klíč a stav je „adopted“.

Bezpečnostní detaily: token se po použití zneplatní; bootstrap skript se stahuje přes ověřené HTTPS (`check-certificate=yes` s veřejným certifikátem check-in hostu); odpojení zařízení (unadopt) vše vrací a revokuje certifikát.

## Sběr dat

Worker (asyncio, omezený počet souběžných spojení) se ptá každého zařízení zvlášť přes REST API nad tunelem. Intervaly jsou odstupňované podle toho, jak rychle se data mění.

| Interval | Data | Zdroj v RouterOS |
| --- | --- | --- |
| 30–60 s | Čítače rozhraní, CPU/RAM, stav linků, wifi klienti (signál, rate, bytes) | `/interface`, `/system/resource`, `/interface/wifi/registration-table` |
| 5 min | Sousedé, MAC tabulky, ARP, DHCP, STP, chyby portů, PoE, SFP | `/ip/neighbor`, `/interface/bridge/host`, `/ip/arp`, `/ip/dhcp-server/lease`, `/interface/bridge/port`, `/interface/ethernet` |
| Denně / při změně | Export, binární záloha, balíčky, RouterBOOT | `/export show-sensitive`, `/system/backup`, `/system/package`, `/system/routerboard` |

Uložení: **PostgreSQL + TimescaleDB** pro časové řady (retence v řádu měsíců, průběžné agregace hodinové/denní, komprese starších chunků), běžné tabulky pro stav a konfiguraci.

Provoz per klient: na gatewayích lokalit **traffic-flow (IPFIX)** posílaný tunelem na `mtc-collector`; agregace podle IP/MAC, VLAN a lokality. Kvůli kapacitě SSTP posílat jen z gatewayí a s vzorkováním, kde je provoz velký.

Syslog: ze všech zařízení na collector a odtud do Wazuhu (bezpečnostní události, přihlášení, změny).

## Korelace a topologie

Topologie lokality se skládá ze snapshotu všech jejích zařízení ve stejném časovém okně (5 min) a výsledkem je graf: zařízení, porty, linky (drát / rádio) a na nich zavěšení klienti.

1. **Hrany** z `/ip/neighbor` (MNDP/LLDP): „A ether5 ↔ B sfp1“. Discovery zapnuté jen na infrastrukturních portech.
2. **Uplinky**: porty, na kterých je soused, se označí jako infrastrukturní.
3. **Drátoví klienti**: MAC z bridge host tabulek; poloha = port, kde je MAC vidět a který není uplink. MAC jen na uplinkách = za nespravovaným switchem → okrajový port s nejvíc MAC, v mapě jako „unmanaged switch“.
4. **Wifi klienti**: přesně z registration table (AP, rozhraní, SSID, signál); VLAN z datapathu nebo bridge host.
5. **Rádiové spoje**: registration table na obou koncích (MAC protistrany, signál, CCQ, rate) → hrana typu rádio s metrikou kvality.
6. **Identita klienta**: IP a hostname z DHCP leases a ARP gatewaye.
7. **Provoz**: wifi bytes z registration table; drát z čítačů portu, pokud je na portu jediný klient; jinak z IPFIX podle IP.
8. **STP**: root bridge z `/interface/bridge/monitor`, role a stav portů z `bridge/port`; alert na změnu rootu a časté přepínání portů.
9. **Chyby portů**: delty čítačů (FCS, align, too-short, link-downs) za interval; alert na růst, ne na absolutní hodnotu.

Historie polohy klientů se ukládá jako intervaly (MAC, port, VLAN, od–do), takže jde zpětně dohledat „kde byl klient X ve 14:00“.

## Bezpečná změna konfigurace

Každá změna, která může odříznout zařízení (VLAN, bridge, firewall, wifi frekvence, adresace), jde přes automatický rollback; bez potvrzení se zařízení samo vrátí.

1. Controller vytvoří zálohu (export + `.backup`) a uloží ji.
2. Naplánuje na zařízení scheduler „za N minut (výchozí 5) obnov zálohu / spusť revert skript“.
3. Aplikuje změnu jako jeden `.rsc` skript.
4. Čeká, až se zařízení ozve přes tunel nebo check-in a projde kontrolou (API odpovídá, sousedé na místě).
5. Úspěch → zruší revert scheduler a zapíše změnu do auditu. Neúspěch → zařízení se samo vrátí, změna je označena jako selhaná.

Pravidla:

- Controller pracuje s **požadovaným stavem** (desired state) a sám spočítá diff vůči skutečnému stavu.
- Změny na víc zařízeních jdou **od nejvzdálenějšího** k nejbližšímu (podle topologie).
- Management VLAN a cesta k controlleru jsou chráněné: změnu, která by je přerušila, controller odmítne.
- Volitelně „dry run“: zobrazit vygenerovaný skript a diff bez aplikace.

### Implementace (commit-confirm, obdoba UniFi SafeOps)

Plán s `revert_script` (zatím modul `ports`) se aplikuje takto (`routeros.safe_apply_body`):

1. Na zařízení se uloží skript `mtc-revert` (návrat do stavu z inventury + nahlášení
   `status=reverted` + úklid) a `mtc-confirm-tick`, scheduler `mtc-confirm` ho spouští každou minutu.
2. Tik přičítá globální počítadlo; po `confirm_minutes` (výchozí 5) ticích spustí návrat.
   Nezávisí na tom, kdy RouterOS scheduler poprvé spustí, ani na půlnoci; po restartu se počítá znovu.
3. Provede se změna a pošle se ack → plán `confirming`, termín `confirm_deadline`.
4. Další check-in zařízení (cesta ke správě funguje) → controller pošle `confirm_plan`
   (smaže scheduler a skripty) → plán `applied`.
5. Když se zařízení neozve, po N minutách se samo vrátí a nahlásí `reverted` → plán
   `rolled_back`, alert `change_rolled_back`. Plán v `confirming` po termínu → alert `change_unconfirmed`.

Ochrany: plán propadne, když se konfigurace portů od jeho vzniku změnila (`portcfg.config_basis`);
řádky bridge VLAN s více ID nebo rozsahem se neupravují; změna uplinku (root port STP, port ke
spravovanému zařízení) dostane varování. Změnu smí zadat technik, aplikovat (`apply`) jen správce.

## Moduly

### Firmware

- Kanály stable / long-term per tenant nebo lokalita; verze schvaluje admin.
- Postupný rollout: testovací zařízení → jedna lokalita → zbytek; automatická pauza, když se zařízení po upgradu neozve.
- Před upgradem záloha; po upgradu RouterOS upgrade RouterBOOT (`/system/routerboard/upgrade`) a druhý reboot.
- Kontrola balíčků: `wifi-qcom` / `wifi-qcom-ac` vs. `wireless` se nesmí omylem vyměnit.
- Okna údržby per lokalita.

### Zálohy

- Denně a před každou změnou: `/export show-sensitive` + `.backup` s heslem; zařízení je nahraje přes `/tool fetch upload=yes` na check-in endpoint.
- Verzování, textový diff exportů, detekce driftu (ruční změny mimo controller).
- Šifrování na disku; retence např. 30 denních + 12 měsíčních.
- Obnova z controlleru jedním klikem (přes rollback mechaniku).

### VLAN

- VLANy se definují na lokalitě (ID, jméno, účel: mgmt, zaměstnanci, hosté, kamery…).
- Porty mají roli trunk / access / hybrid; controller generuje `/interface/bridge/vlan`, `bridge/port` (pvid, frame-types, ingress-filtering) a zapne `vlan-filtering=yes`.
- Kontrola cesty: VLAN musí být povolená na všech uplinkách od AP/portu až ke gatewayi (z topologie).
- Management VLAN je chráněná a změny jdou vždy s rollbackem.

### WiFi CAPsMAN

- Na lokalitě jedno zařízení jako CAPsMAN manager, ostatní AP jako CAP; controller spravuje konfiguraci manageru (SSID, zabezpečení, datapath VLAN, kanály, provisioning). Wifi tak jede i bez spojení s controllerem.
- Podpora obou generací: nový `/interface/wifi` (wifi-qcom) a starý `/caps-man` (wireless); každá pod jiným managerem.
- Monitoring: klienti per AP a SSID, signál, roaming mezi AP, vytížení kanálů.

### WiFi PtP

- Monitoring obou konců: signál, noise, SNR, CCQ, TX/RX rate, latence a ztrátovost přes spoj, historie a trend.
- Alerty na pokles pod práh nebo proti dlouhodobému průměru.
- Změny (frekvence, šířka, protokol) vždy nejdřív na vzdáleném konci, pak na bližším, oba s rollbackem.

## Datový model

Všechny tabulky kromě globálních číselníků nesou `tenant_id` a jsou pod row-level security.

| Tabulka | Klíčové sloupce | Poznámka |
| --- | --- | --- |
| `tenant` | id, name, tunnel_block | Firma |
| `site` | id, tenant_id, name, address, maintenance_window | Lokalita |
| `device` | id, tenant_id, site_id, serial, model, ros_version, role, tunnel_ip, state | state: pending / adopted / offline / removed |
| `device_secret` | device_id, kind, ciphertext, rotated_at | Hesla, tokeny, certifikáty, šifrovaně |
| `interface` | id, device_id, name, type, mac, is_uplink | |
| `link` | id, site_id, a_interface_id, b_interface_id, kind, quality | kind: wired / radio |
| `vlan` | id, site_id, vid, name, purpose | |
| `port_role` | interface_id, mode, pvid, tagged_vlans | Desired state VLAN |
| `client` | id, tenant_id, mac, hostname, last_ip | |
| `client_location` | client_id, interface_id, vlan, valid_from, valid_to | Historie polohy |
| `metric_*` | time, device_id / interface_id / client_id, hodnoty | Timescale hypertabulky |
| `backup` | id, device_id, taken_at, kind, path, sha256, diff_from | |
| `change` | id, device_id, user_id, script, status, rollback_at | Fronta a historie změn |
| `audit_log` | id, time, user_id, tenant_id, action, target, before, after | Append-only |
| `user`, `role_binding` | user_id, role, scope (tenant / site) | RBAC |

## Další vylepšení

### Bezpečnost

| Vylepšení | Co řeší | Priorita |
| --- | --- | --- |
| PAM: Winbox/SSH jen přes controller | Technik nezná hesla zařízení; relace přes controller s nahráváním a časovým limitem | Vysoká |
| Baseline profil + compliance skóre | Pravidelná kontrola hardeningu, hlášení odchylek, volitelně automatická náprava | Vysoká |
| Schválení čtyřma očima | Firmware rollout, změna firewallu nebo mgmt VLAN na víc zařízeních vyžaduje druhou osobu | Vysoká |
| Rotace hesel a certifikátů | Servisní účty a klientské certifikáty automaticky obnovované, např. 90 dní | Vysoká |
| Šifrovaná offsite záloha controlleru | Záloha DB, klíčů a CA mimo server; testovaná obnova | Vysoká |
| Webové UI jen z VPN / admin sítě | Útočná plocha z internetu je jen check-in API | Vysoká |
| CVE hlídání RouterOS | Porovnání verzí zařízení s databází zranitelností | Střední |
| Detekce rogue DHCP / rogue AP | Alert na neznámý DHCP server, wifi scan proti podvrženým SSID | Střední |
| Neznámý klient na mgmt VLAN / access portu | Alert při nové MAC; později 802.1X přes RADIUS controlleru | Střední |
| Detekce smyček a MAC flappingu | Z bridge host historie a STP topology changes | Střední |
| Penetrační test a threat model | Před ostrým nasazením u zákazníků | Střední |

### Funkce

- Šablony konfigurace (profily) per lokalita a role zařízení.
- Notifikace: e-mail, Nextcloud Talk (webhook bota), Wazuh; potlačení duplicit a eskalace.
- Vzdálený PoE restart AP a portů přímo z mapy.
- Půdorysy budov s umístěním AP a heatmapou klientů.
- Bandwidth test mezi zařízeními a z gatewaye ven.
- Read-only portál pro zákazníka (pozdější fáze; UI je zatím jen pro admina).
- Inventář: sériová čísla, záruky, umístění, nákupní data.
- Veřejné REST API controlleru pro integrace.

## Roadmapa

| Fáze | Obsah |
| --- | --- |
| F0 Základ (MVP) | Koncentrátor, adopce, check-in, tunel per zařízení; RBAC + MFA, audit log, šifrovaná tajemství, zálohy |
| F1 Monitoring | Polling, TimescaleDB, dashboard; alerty (offline, chyby portů, STP, CPU/RAM), notifikace |
| F2 Topologie | Mapa ze sousedů a MAC tabulek, poloha klientů, VLAN přehled; IPFIX, provoz per klient |
| **Brána** | Rollback ověřený v laboratoři na všech typech zařízení |
| F3 Správa | Desired state + rollback, VLAN konfigurace, drift; firmware rollout, PAM, schvalování |
| F4 WiFi a compliance | CAPsMAN (obě generace), PtP; baseline compliance, CVE, rogue DHCP/AP |

Fáze F0–F2 jen čtou ze zařízení, takže nemůžou nic rozbít; riziko výpadku nese až F3.

### Stav k 18. 10. 2026

| Fáze | Hotovo | Zbývá |
| --- | --- | --- |
| F0 | vše kromě vlastní CA/klientských certifikátů; `mtc-admin doctor`, `device-check` | klientské certifikáty SSTP |
| F1 | inventura, metriky + grafy, alerty (offline, tunel, REST, CPU/RAM, porty, half-duplex, DHCP pool, NTP, zálohy, verze, bezpečnost), notifikace (webhook, Talk, e-mail), stav workeru | TimescaleDB není potřeba (hodinové agregace v PostgreSQL) |
| F2 | topologie, klienti s polohou a historií 90 dní, hledání, VLAN analýza, porty (rychlost/PoE/VLAN/STP), STP přehled, Wi-Fi přehled | IPFIX / provoz per klient |
| Brána | – | ověřit rollback (test-plan 9b, body 5–6) |
| F3 | NTP plány se schválením; aktualizace RouterOS/RouterBOOT jednotlivě i postupně po lokalitě; release notes; úprava zařízení; správa uživatelů | rollback, VLAN aplikace, firewall, desired state |
| F4 | bezpečnostní audit konfigurace (baseline) | správa CAPsMAN/SSID, PtP, CVE, rogue DHCP/AP |

## Implementace F0 – rozhodnutí a odchylky od návrhu

| Téma | Návrh | Implementace | Důvod |
| --- | --- | --- | --- |
| Web framework | FastAPI | Starlette (FastAPI je nad ním) | Celý stack z Debian apt, bez PyPI |
| DB vrstva | SQLAlchemy + Alembic | psycopg + ruční SQL + vlastní migrátor (`sql/NNNN_*.up.sql`) | Méně vrstev, RLS a triggery jsou v SQL stejně |
| Autentizace check-inu | HMAC podpis | Bearer klíč (256 bitů, v DB hash) přes ověřené TLS | RouterOS skriptování neumí HMAC |
| Klientské certifikáty SSTP | Ano | Zatím ne (unikátní heslo per zařízení + ověřený certifikát serveru) | Další krok; vlastní CA je v plánu |
| REST koncentrátoru | – | Port 8443, jen z IP controlleru, pinning otisku certifikátu | 443 patří SSTP |
| Správa do tunelu | Routa na controller na zařízení | Koncentrátor src-natuje controller na 10.200.0.1 | Zařízení nepotřebuje routu, check-in nikdy neskončí v tunelu, zařízení nevidí IP controlleru |
| Ověření zařízení | – | Otisk certifikátu www-ssl hlásí zařízení přes autentizovaný check-in, REST přes tunel ho pinuje | Žádná slepá důvěra (TOFU) |
| Servisní účet na zařízení | – | `mtc-ctl`, skupina `mtc` (api, rest-api), přihlášení jen z 10.200.0.1 | Cizí účet stejného jména se nepřepíše |
| Příkazy | Fronta skriptů | Fronta `kind + params`, skript se renderuje až při doručení | Tajemství nejsou ve frontě, jen šifrovaně v `device_secret` |
| Failover koncentrátoru | Skript na zařízení | Řídí controller: 5 check-inů s neběžícím tunelem → příkaz k přepnutí | Logika na jednom místě; routy /32 přes aktivní koncentrátor udržuje worker (`tunnel.manage_routes`, modul `routes.py`, jen vlastní routy `proto 186`, CAP_NET_ADMIN drop-in) |

## Sledování nových verzí RouterOS

- Dostupnou verzi zjišťuje každé zařízení samo ve svém kanálu (`check-for-updates`
  přes REST, jednou za 24 h); controller ji vidí v inventuře (`update`).
- Známé problémy verzí (`ros_versions.KNOWN_ISSUES`) se zobrazují u zařízení; aktualizaci
  na verzi s kritickým problémem controller odmítne.
- Changelogy (`releases.py`, tabulka `ros_release`, migrace 0005) stahuje worker jednou
  denně z download.mikrotik.com pro všechny mezilehlé verze přechodů ve flotile.
  Globální data: číst smí každý, zapisovat jen provozovatel (RLS, kontrola 15).
  U zařízení se ukazují důležité změny a změny komponent, které zařízení používá.

## DHCP snooping a cizí DHCP servery

Bridge s `dhcp-snooping=yes` zahodí DHCP odpovědi (OFFER/ACK) z portu bez
`trusted=yes`. Trusted musí být port, kterým vede cesta k legitimnímu DHCP
serveru, a ten je na každém switchi jiný. Controller ho proto počítá z topologie
lokality (`dhcpguard.py`):

- **zdroje DHCP**: DHCP servery a relay na spravovaných zařízeních; cíl relay
  (např. Windows DHCP) se dohledá podle IP mezi adresami zařízení a v tabulce
  klientů (zařízení + port); DHCP klient switche/AP na bridgi (jeho server);
  ruční zdroje v politice lokality `dhcp_snooping` (zařízení, port, IP),
- **graf** spravovaných zařízení ze sousedů MNDP/LLDP s porty na obou koncích,
- **trusted** = první port na cestě k danému zdroji (jen porty v bridgi),
  snooping jen na bridgích, kudy DHCP teče (trusted port nebo lokální server/relay),
- při neznámé cestě nebo nedohledaném serveru se plán pro zařízení nevytvoří
  (snooping by DHCP odřízl); existující trusted porty se nikdy neruší,
- na routerech s DHCP serverem plán přidá `/ip dhcp-server alert` (comment `mtc`);
  `unknown-server` v inventuře → kritický alert `rogue_dhcp`.

Pořadí aplikace: trusted porty → `dhcp-snooping=yes` → alert; návrat opačně.
Plán jde přes commit-confirm, ale **výpadek DHCP správu neodpojí** (tunel jede
dál), takže automatický návrat chybu v DHCP nezachytí – proto jen při známé
cestě a s ověřením na labu (test-plan 9b, bod 9). HW podpora snoopingu je
dokumentovaná pro CRS3xx/CRS5xx/CCR2116/CCR2216, u ostatních plán varuje.

## DHCP leases a statické IP

Statické IP = rezervace v DHCP serveru MikroTiku (statický lease). Přehled serveru
(`leases.py`, Lokalita → Sítě a DHCP → klik na počet leases) spojuje:

- leases daného serveru (rezervace, dynamické) s popisem, DHCP options,
  address-listy a blokováním,
- zařízení s **ručně nastavenou IP** – jsou v ARP v podsíti, ale ne v leases,
- výrobce podle MAC z registru IEEE (Debian balík `ieee-data`, `oui.py`;
  náhodné „soukromé“ MAC se poznají podle lokálně administrovaného bitu),
- polohu z korelace klientů (switch + port, AP + SSID + signál),
- **poznámku** z controlleru (`client_note`, per tenant a MAC – na zařízení se nepíše),
- mapu adres podsítě (do /22), konflikty (stejná IP na více MAC) a ruční IP v poolu,
- návrh volné adresy mimo pool.

Změny jsou plán modulu `dhcp` (observe/manage) s návratovým skriptem a
commit-confirm: dynamický lease → `make-static` + `set`, nová rezervace → `add`,
zrušení → `remove` (návrat ho vrátí se všemi hodnotami), nová DHCP option
`/ip dhcp-server option add … comment="mtc"` (hodnoty: IP `'a.b.c.d'`, text `s'…'`, hex `0x…`).
Kontroly: IP v podsíti, ne brána/síť/broadcast, nepoužitá jiným leasem ani v ARP.
Otisk plánu počítá jen statické leases a options (dynamické se mění pořád).

## Postupná aktualizace lokality

`upgrades.py`, tabulka `upgrade_job` (migrace 0007, RLS, kontrola 16). Kroky v jsonb,
v lokalitě nejvýš jedna běžící úloha (unikátní index). Worker každou minutu: běžící
krok je hotový, až příkaz potvrdil `done` a zařízení se ozvalo s cílovou verzí; při
`failed` nebo po 30 min se úloha zastaví. Další krok jde přes `services.request_command`,
tedy se stejnými kontrolami jako ruční aktualizace. Pořadí podle role (ap, ptp, unknown,
switch, gateway) – restart nikdy neodřízne cestu k ještě neaktualizovaným zařízením.

## Moduly a převzetí stávající konfigurace (F1)

Controller nesmí nic převzít ani předělat sám od sebe. Platí tato pravidla:

1. **Adopce mění jen objekty `mtc`** (tunel `mtc-sstp`, účet `mtc-ctl`, skupina `mtc`,
   check-in skript, firewall pravidla s komentářem `mtc:`). Nic jiného.
2. **Inventura je jen čtení**: každých 5 minut přes REST (pinning) – systém, rozhraní,
   bridge, porty, VLAN, adresy, sousedé, NTP, hodiny, balíčky, dostupná aktualizace.
   Hesla se nečtou (uživatelé, PPP secrety, wifi klíče) a klíče s hesly se filtrují.
3. **Zálohy**: denně `/export show-sensitive` přes REST, uložené šifrovaně. Změna
   konfigurace mimo controller se zapíše do auditu (`device.config_changed`, drift).
4. **Režim modulu per zařízení**: `observe` (výchozí – jen hlásí odchylky) nebo
   `manage` (smí zapisovat). Přepnutí smí jen tenant_admin a je auditované.
5. **Změny jen přes plány**: požadovaný stav → diff proti inventuře → plán (skript +
   popis) → schválení → doručení frontou příkazů → potvrzení. Plán je vázaný na
   konkrétní stav zařízení (`basis_sha`); když se zařízení mezitím změní, plán propadne.
   Pro moduly v `security.four_eyes_modules` (výchozí VLAN, firewall) musí schválit
   jiný uživatel než autor.

### VLAN a bridge

Cílový model: **jeden bridge s `vlan-filtering=yes`**, tagování v `/interface bridge vlan`,
přístupové porty přes `pvid`, L3 přes VLAN rozhraní na bridge, `ingress-filtering`
zapnutý. Analýza (`analysis.analyze_vlan`) hlásí:

| Kód | Závažnost | Význam |
| --- | --- | --- |
| `multiple_bridges` | warning | víc bridgů s porty (legacy) |
| `vlan_iface_bridged` | warning | VLAN rozhraní jako port bridge (legacy) |
| `vlan_on_physical` | warning/info | VLAN rozhraní na fyzickém portu (v bridge = obchází ho) |
| `nested_bridge` | error | bridge jako port jiného bridge |
| `bridge_vlan_without_filtering` | warning | VLAN na bridge bez filtrování |
| `bridge_not_tagged` | error | VLAN rozhraní na bridge, ale bridge v té VLAN není tagovaný |
| `ingress_filtering_off` | warning | riziko VLAN hoppingu |

Legacy zapojení se **nepředělává automaticky**. Pro vzor „bridge na každou VLAN + VLAN
rozhraní na trunku" umí controller vygenerovat **náhled migrace** (kroky + skript);
složitější zapojení vrací důvody, proč automaticky nejde. Spouštění migrace přijde,
až bude ověřené v laboratoři (s rollbackem, od nejvzdálenějšího zařízení, v okně údržby
a s ohledem na HW offload – na některých starších čipech vlan-filtering přepne provoz na CPU).

### NTP

Politika lokality: primární zařízení (synchronizuje upstream, typicky `tik/tak.cesnet.cz`,
a běží jako NTP server), ostatní zařízení berou čas z primárního (adresa ve stejné síti)
s prvním upstreamem jako zálohou, všude stejná časová zóna. U klientů se NTP server
nemění. NTP je první zápisový modul – ověřujeme na něm celý mechanismus plánů.

### Historie portů (Time Machine)

`porthistory.py` při každé úplné inventuře (obsahuje `interfaces`) porovná stav
fyzických portů s předchozí inventurou a změny zapíše do `port_event` (retence
90 dní). Vlastnost se porovná jen tehdy, když její zdrojová sekce je v obou
inventurách – selhání jednoho REST dotazu nevyrobí falešné události. Výpadky
linky mezi inventurami se poznají z přírůstku `link-downs`.

### Firewall

Zobrazení jako zone-based firewall UniFi (`firewall.py`, jen čtení): zóna = vlastní
`/interface list`, buňka matice zdroj → cíl = chain `forward`, zdroj → Router =
`input`. Rozhoduje první pravidlo platné pro celou dvojici zón bez dalších podmínek
(port, protokol, adresa, čas, ipsec…); podmíněná pravidla před ním jsou výjimky,
bez pravidla platí výchozí accept. `established,related` a `invalid` se ukazují zvlášť.
Členem zóny může být rozhraní nebo jiný seznam (rozvine se o jednu úroveň);
`!LAN` = všechny zóny kromě LAN. Pravidla s `in-interface` mimo zóny se ukazují
jen v seznamu pravidel.

Plán správy:

1. address-listy spravované controllerem (např. management sítě),
2. vlastní chainy `mtc-*` + jump pravidla, zbytek firewallu ruční,
3. plná správa se šablonami jen tam, kde ji admin výslovně zapne.

### Verze RouterOS

Worker denně nechá zařízení zkontrolovat dostupnou verzi v jeho kanálu
(`/system/package/update/check-for-updates`). Tabulka známých problémů verzí
(`ros_versions.py`) se zobrazuje u zařízení. Hromadný rollout aktualizací je F3.

## Řízený režim lokality (F5)

Stav: **F5a (switch) a F5b (Wi-Fi přes CAPsMAN) implementováno**, čeká na ověření v labu (test-plan 9c, 9d); F5c–F6 jsou návrh.
Cíl: uživatel neřeší bridge, VLAN tabulky
ani CAPsMAN. Nadefinuje sítě lokality, zařízením dá roli a controller z toho sám spočítá
a nasadí celou konfiguraci. Vychází z UniFi, ale bez věcí, které na MikroTiku nejdou
spolehlivě automatizovat.

### Rozsah první verze

| Téma | V první verzi | Později |
| --- | --- | --- |
| Zařízení | RouterOS v7 s ethernet/SFP porty; Wi-Fi jen balíček `wifi-qcom` / `wifi-qcom-ac` (nové `/interface/wifi`) | starý balíček `wireless` (`/caps-man`) – takové AP půjde jen jako switch |
| Role | **switch**, **AP + switch**, **CAPsMAN** (příznak na jednom zařízení lokality) | **router + firewall** (VLAN rozhraní, DHCP servery, zóny) |
| Brána lokality | cizí / stávající router; sítě (VLAN, podsíť, DHCP) poskytuje on | spravovaný MikroTik jako gateway ze stejné definice sítí |
| Sítě na trunkách | všechny sítě lokality na všech trunkách | prořezávání (jen VLAN potřebné za portem) |

Stávající režim „sledování + moduly“ zůstává pro zařízení, která převzít nechceme
(cizí konfigurace, routery zákazníka). Zařízení je buď **sledované**, nebo **řízené**;
řízené má celou konfiguraci od controlleru.

### Záměr lokality (co zadává uživatel)

1. **Sítě**: jméno, VLAN ID, účel (`mgmt`, `lan`, `hoste`, `iot`, `kamery`, `voip`),
   podsíť a brána (informativně – pro DHCP snooping, kontrolu klientů a později router).
   Právě jedna síť je `mgmt`.
2. **Wi-Fi sítě (SSID)**: SSID, heslo (šifrovaně, SecretBox), síť (= VLAN), pásma
   (výchozí 2,4 + 5 GHz pod jedním SSID; `iot` jen 2,4 GHz), hostovská izolace klientů
   (výchozí pro `hoste`). Na rádio nejvýš 4 SSID (každé SSID = vlastní beacony).
3. **Výchozí síť přístupových portů** (typicky `lan`) a případné výjimky na portech
   (stávající profily portů – převedou se z čísel VLAN na odkazy na sítě).
4. **Role zařízení**: switch / AP + switch; CAPsMAN lze zapnout na jednom zařízení
   lokality. Role AP jde vybrat až když lokalita CAPsMAN má (a zařízení má podporovaná rádia).

Pomocník „Načíst ze stávající sítě“ předvyplní sítě z inventury (VLAN rozhraní, adresy,
DHCP servery na gatewayi – `dhcpguard.site_networks`), uživatel jen potvrdí a pojmenuje.

### Převzetí zařízení

Po adopci se zařízení klasifikuje podle inventury:

| Třída | Poznávací znaky | Převzetí |
| --- | --- | --- |
| `factory` | defconf nebo prázdná konfigurace | rovnou |
| `simple` | jeden bridge, žádné VLAN, žádné routování kromě DHCP klienta | rovnou |
| `conforming` | jeden bridge s vlan-filtering odpovídající modelu | rovnou |
| `custom` | firewall, NAT, PPPoE, víc bridgů, VPN, routovací protokoly, cizí wifi | jen po výslovném potvrzení se seznamem, co zanikne |

Převzetí **nepřepisuje konfiguraci po kouscích**, ale nasadí ji celou načisto – výsledek je
deterministický a nezáleží na tom, co na zařízení bylo:

1. Záloha `.backup` (s heslem) a `export show-sensitive` do controlleru i na flash zařízení.
2. Controller vygeneruje `mtc-bootstrap.rsc`: celá cílová konfigurace **včetně adopčních
   objektů** (SSTP klient, `mtc-ctl`, certifikát REST, check-in, firewall `mtc:`).
3. `/system reset-configuration no-defaults=yes skip-backup=yes run-after-reset=mtc-bootstrap.rsc`.
4. Bootstrap nejdřív počká na rozhraní (jako defconf: smyčka s `:delay` dokud nejsou porty),
   pak nastaví vše, naplánuje **návrat**: když do N minut (výchozí 10) nepřijde potvrzení
   z controlleru, `/system backup load` původní zálohy. Na konci smaže sám sebe
   (obsahuje tajemství).
5. Zařízení se ozve přes tunel → controller ověří (REST, sousedé, porty) → potvrdí → hotovo.

K ověření v labu: co reset dělá s certifikáty a soubory na flash (cert REST se v bootstrapu
vytváří znovu, `.rsc` a `.backup` na flash zůstávají), časování `run-after-reset` na
switchích CRS a chování `backup load` jako návratu.

Po převzetí controller hlídá drift (export vs. vygenerovaná konfigurace). Ruční změna
na zařízení → alert a plán „vrátit do záměru“ ke schválení, nic automaticky.

### Cílová konfigurace switche

- Jeden `bridge` se všemi ethernet a SFP porty, `vlan-filtering=yes`, `protocol-mode=rstp`,
  `igmp-snooping=yes`, `dhcp-snooping=yes` (trusted jen uplink), HW offload zachován
  (CRS3xx/5xx umí vlan-filtering v HW; u zařízení bez HW VLAN controller upozorní,
  že provoz jde přes CPU).
- **Management**: L3 jen v síti `mgmt` – `vlan-mgmt` na bridgi (nebo bridge sám, když
  je mgmt nativní), DHCP klient nebo statická adresa z IPAM. Žádná jiná IP.
- **Uplink**: port směrem k bráně. Před převzetím se určí z inventury: port, na kterém
  bridge host tabulka vidí MAC výchozí brány (u RSTP root port). Bootstrap musí zachovat
  přesně ten způsob, kterým zařízení dnes dosáhne internetu (nativní / tagovaná mgmt),
  jinak se neozve – to kontroluje controller před vygenerováním.
- **Trunky** = uplink + porty se sousedem, který je spravovaný MikroTik nebo hlásí
  přes LLDP/MNDP schopnost `bridge`/`wlan-ap`: všechny sítě tagované, mgmt podle volby
  lokality nativně (`pvid`) nebo tagovaná, `ingress-filtering=yes`.
- **Přístupové porty** = vše ostatní: `pvid` výchozí sítě, `frame-types=admit-only-untagged-and-priority-tagged`,
  edge port, loop protect.
- Neznámý hloupý switch (víc MAC na portu, bez LLDP) zůstává přístupový – bezpečnější.

### Automatická distribuce VLAN

Topologie lokality (MNDP/LLDP, `dhcpguard.site_graph`) je strom od brány. Pro každé
řízené zařízení se z ní spočítá role portů (výše) a z ní bridge VLAN tabulka:

```
pro každou síť v:
  tagged   = trunky, na kterých v není nativní  (+ bridge, pokud v = mgmt a je tagovaná)
  untagged = přístupové porty s pvid v + trunky s nativní v
```

Protože v první verzi jdou na trunky všechny sítě, přidání sítě = plán na všechna řízená
zařízení lokality (od nejvzdálenějšího, s návratem). Nový spravovaný soused na přístupovém
portu → plán „port → trunk“; jen **přidává tagované VLAN a zachovává pvid**, takže stávající
provoz na portu nepřeruší. Tuhle třídu změn může lokalita povolit aplikovat automaticky
(volba „automaticky rozšiřovat trunky“); vše ostatní jde přes schválení.

Kontroly před aplikací: VLAN sítě musí existovat na uplinku brány (z inventury brány,
pokud je sledovaná; jinak upozornění „ověř na routeru“), mgmt cesta k controlleru se
nesmí změnit, mezi řízenými zařízeními nesmí být neřízený hop s neznámým VLAN chováním.

### Wi-Fi: CAPsMAN, AP + switch

Proč CAPsMAN a ne konfigurace každého AP zvlášť: podle dokumentace MikroTiku klient
roamuje s 802.11r (FT) jen mezi AP řízenými **stejnou instancí RouterOS**. Bez CAPsMANu
by FT fungovalo jen mezi rádii jednoho AP.

- **CAPsMAN host**: doporučí se nejstabilnější zařízení (core switch / root bridge),
  ne AP. Potřebuje IP v `mgmt`. Nová `wifi` CAPsMAN umí jen lokální forwarding, takže
  provoz klientů jde přímo z AP do bridge – CAPsMAN není v datové cestě.
- **Na CAPsMANu** (vše s komentářem `mtc`):
  - `/interface wifi security` per SSID: `authentication-types=wpa2-psk,wpa3-psk`,
    `ft=yes ft-over-ds=yes`, `ft-mobility-domain` odvozený z SSID, `management-protection=allowed`;
  - `/interface wifi datapath` per síť: `bridge=bridge vlan-id=<VID>`, u hostů `client-isolation=yes`;
  - `/interface wifi steering`: `rrm=yes wnm=yes` (802.11k/v), společná `neighbor-group` pro SSID;
  - `/interface wifi channel` + `configuration` **per rádio** (kanál a šířka z plánu níže),
    `country=Czech Republic`;
  - `/interface wifi provisioning` per rádio podle `radio-mac`: master = první SSID,
    slave = ostatní SSID pro dané pásmo;
  - `/interface wifi capsman set enabled=yes` jen na `vlan-mgmt`, certifikáty `auto`,
    `require-peer-certificate=yes` po prvním spárování (CAP se nedá podvrhnout).
- **Na AP + switch**: konfigurace switche (výše) + `/interface wifi cap set enabled=yes
  caps-man-addresses=<IP CAPsMANu v mgmt> discovery-interfaces=vlan-mgmt
  slaves-datapath=... lock-to-caps-man=yes`. Uplink AP je trunk se všemi sítěmi, datapath
  `vlan-id` z rádia udělá přístupový port dané sítě.
- Rádia na samotném CAPsMAN hostu se řídí stejným CAPsMANem (CAP na `127.0.0.1` – ověřit v labu).

K ověření v labu: dynamické položky bridge VLAN pro wifi rozhraní s `datapath.vlan-id`,
chování CAP při výpadku CAPsMANu (jak dlouho drží SSID), FT s WPA3 a starými klienty
(volba „FT vypnout“ per SSID jako únik).

### Algoritmus přidělení pásem a kanálů

Rozšíření současného `channels.py` (dnes: všechny AP lokality se slyší, šířka podle počtu AP).

**Vstupy**

- Rádia řízená CAPsMANem lokality, pásma, současný kanál, počet klientů a provoz.
- **RF graf**: noční sken v okně údržby – AP po jednom, každé rádio krátce `/interface wifi scan`
  (přeruší jen to jedno rádio na pár sekund). Z toho RSSI každé dvojice našich AP
  a vytížení kanálů cizími sítěmi. Bez skenu se použije konzervativní předpoklad
  „všechny AP lokality se slyší“ (dnešní chování).
- Politika: povolené kanály EU (36–64, 100–116, 132–140; bez 120–128 meteoradarů),
  DFS ano/ne, maximální šířka.

**Postup (per lokalita a pásmo)**

1. **Hrany**: dvojice AP s RSSI ≥ −82 dBm se ruší, váha roste se signálem
   (−82 → 1, −65 a víc → 4).
2. **Šířka 5 GHz**: nejširší z 80/40/20 MHz, pro kterou počet nepřekrývajících se kanálů
   ≥ (nejvyšší počet silných sousedů jednoho AP + 1). Šířka se tak řídí hustotou,
   ne celkovým počtem AP – 40 AP v rozlehlé budově může jet na 80 MHz.
3. **Barvení grafu (DSatur)**: AP v pořadí „nejvíc už obarvených sousedů, pak nejvíc
   klientů“; každý dostane kanál s nejnižší cenou:
   `cena = Σ váha souseda na překrývajícím se kanálu + cizí vytížení kanálu
   + penále DFS (vyšší u AP označených jako kritické a u AP s historií radaru)
   + penále změny (setrvačnost – současný kanál má slevu)`.
4. **Lokální zlepšení**: opakovaně zkusit pro každé AP lepší kanál; přijmout jen když
   celková cena klesne o víc než práh (např. 15 %) – jinak nic neměnit. Žádné cyklení
   kanálů noc co noc.
5. **2,4 GHz**: jen 1/6/11 a 20 MHz, stejné barvení. Když AP má víc silných sousedů
   na všech třech kanálech, doporučí se na části AP 2,4 GHz vypnout (ta, jejichž oblast
   pokryjí sousedé) – jen doporučení.
6. **Rozdělení klientů mezi pásma**: jedno SSID pro obě pásma, 802.11k/v steering,
   vysílací výkon 2,4 GHz o ~6 dB nižší než 5 GHz (klient sám preferuje 5 GHz a pokrytí
   obou pásem je podobné). Volitelně „odpojit slabé klienty“ (access-list pod −80 dBm)
   proti přilepeným klientům – výchozí vypnuto.

**Výstup a nasazení**: plán s porovnáním před/po (počet kolizí, vážená cena), změny jdou
přes konfiguraci rádia na CAPsMANu, v okně údržby, nejvýš třetina AP najednou (změna kanálu
na chvíli odpojí klienty). První výpočet při zapnutí CAPsMANu, pak jednou za noc jen
když se graf změnil (nové AP, nový cizí silný soused).

### Datový model (nové tabulky, vše s `tenant_id` a RLS)

| Tabulka | Klíčové sloupce |
| --- | --- |
| `site_network` | site_id, name, vlan_id (unikátní na lokalitě), purpose, subnet, gateway |
| `site_ssid` | site_id, network_id, ssid, psk (SecretBox, kontext id SSID), bands, isolation, ft, enabled |
| `site_intent` | site_id, mgmt_native (bool), default_access_network, capsman_device_id, auto_trunk (bool), wifi policy (DFS, max šířka, kritická AP) |
| `device` | + `managed` (sledované / řízené), role rozšířit o `ap_switch` |
| `wifi_scan` | device_id, radio, seen_bssid, rssi, channel, scanned_at (retence pár dní) |

### Fáze

1. **F5a – switch**: sítě lokality, převzetí (bootstrap s návratem), automatické porty
   a VLAN, drift. Lab: hEX + CRS3xx.
2. **F5b – Wi-Fi**: CAPsMAN host, AP + switch, SSID ze sítí, FT/k/v, plán kanálů
   bez skenu (dnešní předpoklad + šířka podle hustoty).
3. **F5c – RF**: noční sken, RF graf, DSatur + lokální zlepšení, výkon 2,4 GHz.
4. **F6 – router + firewall**: z téže definice sítí VLAN rozhraní, DHCP servery, zóny.

### Implementace F5a – rozhodnutí a odchylky od návrhu

| Téma | Implementace | Důvod |
| --- | --- | --- |
| Záměr lokality | `site_policy` module `intent` (sítě, `mgmt_vlan`, `mgmt_native`, `access_vlan`, `auto_trunk`), validace `intent.py` | jeden dokument, bez nové tabulky; SSID s hesly přijdou do vlastní tabulky (F5b) |
| Výjimky zařízení | tabulka `device_intent` (porty pod současnými jmény, management dhcp/static) | RLS jako ostatní data tenanta |
| Management na zařízení | L3 přímo na bridgi, bridge `pvid` = VLAN managementu (netagovaný člen) | stejné pro nativní i tagovanou mgmt a i pro VLAN 1; žádné VLAN rozhraní |
| Způsob adresy | když výjimka nic neříká, zachová se dnešní (DHCP / statická z inventury) | zařízení se po převzetí musí ozvat |
| Trunk | uplink + porty se sousedem MikroTik nebo LLDP schopností bridge/wlan-ap; telefony (telephone) ne | IP telefony hlásí bridge kvůli průchozímu portu |
| Převzetí | plán `takeover`: příkaz udělá `.backup` s náhodným heslem (SecretBox), stáhne bootstrap z `/api/checkin/bootstrap?id=` (jen pro příkaz ve stavu sent) a po ack `reset-configuration keep-users=yes no-defaults=yes run-after-reset` | konfigurace načisto, deterministický výsledek |
| Návrat | bootstrap nejdřív uloží `mtc-revert` (komentář `mtc-takeover`) + tik; po 12 min bez potvrzení `backup load`; chyba konfigurace = návrat hned | polovičatou konfiguraci nesmí jít potvrdit |
| Potvrzení | check-in hlásí `tk=1` (běží bootstrap); controller potvrdí až s `tk=1` a běžícím tunelem; potvrzení na zařízení ověří značku – po návratu ze zálohy selže → `rolled_back` | ozvání staré konfigurace nesmí převzetí potvrdit |
| Certifikát REST | při `tk=1` controller přijme nový otisk (auditováno), při návratu obnoví původní | reset může certifikát smazat |
| Sladění | plán `switch` = rozdíl modelu proti inventuře (porty bridge, položky VLAN, loop protect) s commit-confirm; jen-přidávající plány se při `auto_trunk` schválí samy | automatická distribuce VLAN bez rizika přerušení |
| Flash | soubory převzetí jdou do `flash/`, pokud zařízení tuhle složku má | jinak by v RAM nepřežily restart |

Otevřené (po labu): drift řízených zařízení (alert na ruční změnu), sladění spouštěné workerem
po inventuře (dnes tlačítkem), hardening vstupu na řízeném switchi, CAPsMAN a SSID (F5b).

### Implementace F5b – Wi-Fi přes CAPsMAN

| Téma | Implementace | Důvod |
| --- | --- | --- |
| SSID | tabulka `site_ssid` (migrace 0014, RLS, checks 22): SSID, síť (VLAN), pásma, izolace, FT, zapnuto; heslo jen šifrované (SecretBox, kontext `ssid:<id>`) | tajemství nikdy v plaintextu v DB |
| Heslo ve skriptu | plán `capsman` ukládá jen token `MTCPSK<id>`; heslo se dosadí až při doručení příkazu (`wificfg.fill_secrets`, RLS tenanta) | skript plánu je v DB a v UI |
| Wi-Fi politika | v záměru `wifi`: zařízení s CAPsMANem (jen řízené zařízení lokality), země, DFS, max. šířka 5 GHz | uživatel neřeší kanály ani CAPsMAN |
| Role AP | `device_intent.role` = switch / ap; AP jde vybrat až když lokalita má CAPsMAN a zařízení má balík wifi (wifi-qcom, wifi-qcom-ac); starý `wireless` jen jako switch | jedna generace CAPsMANu |
| CAP | v bootstrapu i ve sladění: `/interface wifi cap` – discovery na bridgi (management), `caps-man-names` = identita hostitele, `lock-to-caps-man` | AP se nepřipojí k cizímu CAPsMANu |
| Manager | objekty `mtc-*` (komentář `mtc`) se nahradí celé: steering (rrm, wnm), per SSID security (WPA2/WPA3-PSK, FT + FT over DS) a datapath (bridge, vlan-id, izolace), per rádio channel + master configuration + provisioning podle radio-mac, další SSID jako slave | deterministický stav; FT funguje jen v rámci jednoho CAPsMANu |
| Kanály | `channels.plan` s politikou (max. šířka, bez DFS → zúžení, dokud se AP nevejdou vedle sebe); kanál per rádio v channel objektu | F5c doplní RF sken |
| Bez návratu | plán `capsman` nemá commit-confirm | změna Wi-Fi nemůže přerušit správu – AP jsou na drátě |

K ověření v labu: názvy vlastností steeringu (`rrm`, `wnm`), dynamické položky bridge VLAN pro
rádia s `datapath.vlan-id` na CAP, řízení vlastních rádií hostitele přes `caps-man-addresses=127.0.0.1`,
`/interface wifi radio provision`, FT s WPA3 a staršími klienty. Nové AP se do provisioningu dostane
dalším „Nasadit Wi-Fi“ (rádia se berou z inventury řízených AP).

## Rychlé doručení změn

Zařízení si příkazy vyzvedává check-inem (scheduler jednou za minutu); změna s návratem
potřebuje ještě jeden check-in na potvrzení. Aby změna neběžela minuty:

- **Kick**: po schválení plánu, po ručním příkazu a po ack (další příkaz nebo potvrzení)
  controller přes REST v tunelu zavolá `/execute` s kódem check-inu – zařízení si příkaz
  vyzvedne hned (`services.kick`, na pozadí, best-effort; když tunel nejede, doručí scheduler).
  Vlastní soubor `mtc-kick.rsc`, aby si s naplánovaným check-inem nemazaly soubor příkazu.
  Provisioning se nešťouchá (mění heslo vlastního účtu mtc-ctl – to smí jen scheduler),
  převzetí se potvrzuje až po resetu.
- **Rychlá inventura**: po ack se zařízení označí k nové inventuře a worker ji mezi koly
  (každých 5 s) načte hned.
- **UI**: `GET …/devices/{id}/activity` – detail zařízení ukazuje průběh (čeká na doručení →
  zařízení provádí → čeká na potvrzení → hotovo) a po dokončení a nové inventuře se sám obnoví.

## Výchozí konfigurace podle role a ohlášení (jako UniFi)

Nový kus se připraví před montáží: Lokalita → Nasazení → výchozí konfigurace zvolené role
(switch, AP, switch + AP, switch + CAPsMAN, firewall). Dvě cesty:

- **Netinstall** `-s mtc-default-ROLE.rsc` – vlastní výchozí konfigurace, platí i po resetu tlačítkem;
- **běžící kus**: one-liner stáhne skript z `/enroll/<klíč>/default/<role>` a provede
  `reset-configuration no-defaults=yes keep-users=yes run-after-reset=` (soubory do `flash/`, má-li ho zařízení).

| Téma | Implementace |
| --- | --- |
| Výchozí stav | jeden `bridge` se všemi porty, `vlan-filtering=yes`, vše ve VLAN 1 bez tagu, DHCP klient na bridgi, NTP, vypnuté telnet/ftp/www/api, MAC server vypnutý; AP: rádia vypnutá (SSID až z CAPsMANu) |
| Firewall | WAN ether1 (DHCP klient), LAN bridge 192.168.88.1/24 + DHCP server, zónový firewall `fwmatrix`: zóny = interface listy, `mtc-admin` address list pro správu, politika LAN → WAN, uvnitř zóny povoleno, jinak výchozí zákaz; input jen služby (DNS/DHCP/NTP) z vnitřních zón a správa z `mtc-admin`; NAT, IPv6 bez přístupu z WAN. Komentáře `mtc-fw:` (ne `mtc:` – to jsou pravidla tunelu) |
| Ohlášení | scheduler `mtc-enroll` každou minutu `GET /enroll/<klíč lokality>?role=…` → adopce krok 1 s rolí → zařízení čeká na schválení (ruční); po instalaci check-inu se scheduler sám odstraní |
| Klíč lokality | `site_policy` module `enroll`: hash (vyhledání) + šifrovaná podoba (zobrazení v UI); víceúčelový, rotace zneplatní staré skripty; jen pro tenant_admin |
| Po adopci | značka `mtc-default:<role>` v komentáři bridge → zařízení je řízené hned (`managed_at`), bez převzetí resetem; role z výchozí konfigurace (`device_intent.role`) |
| Sladění | navíc `pvid` bridge (VLAN managementu) a adresa managementu DHCP ↔ statická (nová adresa dřív, než zmizí stará; s automatickým návratem) |
| Výchozí hesla | tabulka `factory_password` (migrace 0015, RLS, checks 23) podle sériového čísla, šifrovaně, import CSV, zobrazení jen tenant_admin a auditované |

Firewall/router je zatím jen výchozí konfigurace; sladění sítí a editor matice zón přijde s řízením routeru (F6).
K ověření v labu: `netinstall-cli -s` (vlastní výchozí konfigurace přežije reset tlačítkem),
`run-after-reset` se skriptem z `flash/` na starších kusech, FastTrack s `hw-offload` na RB2011/hAP ac².
