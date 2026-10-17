# MikroTik Controller – návrh architektury

Stav k 2026-10-17. Živá verze dokumentu je v Claude Docs; tento soubor je verzovaná kopie u kódu.

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
| Failover koncentrátoru | Skript na zařízení | Řídí controller: 5 check-inů s neběžícím tunelem → příkaz k přepnutí | Logika na jednom místě; routování poolu přes záložní koncentrátor je další krok |

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

### Firewall (plán)

1. address-listy spravované controllerem (např. management sítě),
2. vlastní chainy `mtc-*` + jump pravidla, zbytek firewallu ruční,
3. plná správa se šablonami jen tam, kde ji admin výslovně zapne.

### Verze RouterOS

Worker denně nechá zařízení zkontrolovat dostupnou verzi v jeho kanálu
(`/system/package/update/check-for-updates`). Tabulka známých problémů verzí
(`ros_versions.py`) se zobrazuje u zařízení. Hromadný rollout aktualizací je F3.
