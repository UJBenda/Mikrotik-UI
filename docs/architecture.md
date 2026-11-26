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
   Skript posílá svou verzi (`sv`, `routeros.CHECKIN_REV`); starší controller jednou přeinstaluje
   (příkaz `checkin_update`). Když fetch selže, skript znovu dosadí CA controlleru – stáhne ji
   přes HTTP a ponechá jen se shodným otiskem, nastaví `trusted=yes` (smazaná / nedůvěryhodná CA
   by jinak check-in zastavila natrvalo). Zařízení, které už CA nemá a staré skripty tu opravu
   neumí: `mtc-admin device-trust` vypíše příkaz pro jeho terminál; `mtc-admin doctor` hlásí,
   když nginx předkládá zařízením certifikát mimo CA controlleru.

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

### Prázdné zařízení (smazaná konfigurace)

Adopční příkaz i příkaz výchozí konfigurace role začínají `net_bootstrap_body`: když zařízení
nemá žádnou IP ani DHCP klienta, založí `bridge` se všemi ethernety, DHCP klienta na bridgi
(komentář `mtc bootstrap`), počká na adresu (60 s, jinak `:error`) a zapne NTP (zařízení bez
RTC začíná v roce 1970 a ověření certifikátu controlleru by selhalo). Zařízení s IP nemění.
Příkaz se vkládá přes Winbox (MAC) nebo sériovou konzoli.

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
- **Příprava VLAN lokality** (`vlanprep.py`): adoptovaný switch bez převzetí načisto dostane jedním
  plánem portů (commit-confirm) všechny sítě lokality do `/interface bridge vlan` – na uplinku
  tagovaně (STP root port, port ke spravovanému sousedovi, port za kterým bridge vidí MAC výchozí
  brány), bridge sám jen u VLAN s vlastní adresou zařízení – a nakonec `vlan-filtering=yes`.
  Porty zůstávají v PVID 1. Detail zařízení vrací `site_vlans` (chybějící VLAN), UI ukáže banner
  „Připravit VLAN lokality“; endpoint `POST …/devices/{id}/bonds` s `kind=site_vlans`.
- **Vestavěný profil „Trunk – všechny VLAN“**: v `port_profile` s `builtin=true`, PVID 1 netagovaně
  a `tagged='*'` = všechny sítě lokality zařízení kromě PVID; `*` se rozvine až při použití
  (`profiles.expand`), takže nová síť lokality se do profilu promítne sama. Založí se
  tenantovi při prvním výpisu profilů, nejde smazat (migrace 0022).

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
   Pro moduly v `security.four_eyes_modules` (výchozí jen firewall) musí schválit
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

### Skupiny AP, více hesel na SSID, VLAN rádií podle MAC

Proč: na lokalitě s desítkami AP chce správce na různých AP různé sady SSID, na cAP ax jedno
SSID s více hesly (každé heslo jiná VLAN) a starší cAP ac neumí VLAN v `datapath` (802.11ac
`vlan-id` nepodporuje) a jména jeho slave rozhraní se mění (`wifi3`/`wifi4`… podle pořadí vytvoření).

| Téma | Implementace | Důvod |
| --- | --- | --- |
| Skupiny AP | záměr `wifi.groups` `[{id, name}]` (id `g-…`), AP → skupina v `device_intent.ap_group` (migrace 0016), SSID → `site_ssid.groups` (`[]` = všechna AP); zrušená skupina vrátí SSID i AP na „všechna AP“ | skupiny jsou součást záměru, ne objekt RouterOS |
| Kde se SSID vysílá | `wificfg.manager` dává rádiu jen SSID jeho skupiny a pásma; >4 SSID na rádio varování, >8 překážka | beacony každého SSID berou vysílací čas |
| Více hesel | `multi-passphrase` skupina `mtc-mp<n>`, security jen `wpa2-psk` (WPA3 multi-passphrase neumí), FT zůstává; hesla šifrovaně jako jeden blob (kontext `ssid:<id>:ppsk`), jména a VLAN v `ppsk_meta` (bez hesel – kontrola sítí záměru bez dešifrování); ve skriptu token `MTCMP<id>X<i>`, heslo se dosadí až při doručení (jeden průchod – dosazené heslo se znovu neprohledává) | tajemství nikdy v DB ani v plánu v plaintextu |
| ax vs. ac | SSID s více hesly se na rádio bez `wifi-qcom` (čip z `packages.chip`) nedá, s poznámkou v náhledu i na kartě SSID | klient s heslem s VLAN by se na ac nepřipojil |
| VLAN rádií | datapath jen `client-isolation` (bez `bridge`/`vlan-id`); na AP skript `mtc-wifi-vlan` (scheduler 20 s) s tabulkou MAC → `pvid|tagged`: najde rozhraní podle MAC, dá ho do bridge s `pvid` (komentář `mtc-wifi`), tagovaně do VLAN z více hesel, porty `mtc-wifi` mimo tabulku odebere | jeden mechanismus pro ax i ac; MAC rozhraní jsou stálé (`slaves-static=yes`), jména ne |
| Tabulka | z inventury CAPsMANu (rozhraní CAP: MAC + `configuration`) a `config_map` posledního nasazeného plánu `capsman` (`mtc-s<n>` / `mtc-r-<mac>` → SSID); otisk tabulky v komentáři skriptu `mtc-wifi:<sha>` (inventura sekce `scripts`, jen jméno a komentář) | controller pozná, že AP má jinou tabulku |
| Sladění | worker krok `wifitable`: kde se otisk liší, plán `wifitable` (základ = otisk nainstalované tabulky) a hned schválení; stejná tabulka se 15 min po nasazení neplánuje znovu | jen porty rádií v bridgi – správu přerušit nemůže |
| Switch | `switchcfg.reconcile` nechává ne-fyzické členy VLAN (rádia) na pokoji | sladění switche nesmí rozbít Wi-Fi |
| UI | karty SSID s editorem (skupiny, více hesel, import CSV, odhalení s auditem), tabule skupin AP s přetahováním, kontextové menu (pravé tlačítko / ⋯ / klávesa Menu) na SSID, AP, skupinách, zařízeních, plánech a leasech | méně klikání, akce tam, kde je objekt |

K ověření v labu: že CAPsMAN v inventuře `/interface/wifi` ukazuje rozhraní CAP s `mac-address`
a `configuration`, syntaxe `multi-passphrase` (verze RouterOS), chování `slaves-static` po restartu
CAPu, FT s více hesly na ax (podle provozní zkušenosti funguje).

### Jméno zařízení (identity)

Jméno (`/system identity`) se zadává v controlleru: při schválení adopce (pošle se hned po
provisioningu) nebo později v přehledu (dvojklik, pravé tlačítko) a v detailu. Jen ASCII
`[A-Za-z0-9][A-Za-z0-9._-]{0,62}`, na lokalitě jedinečné. Příkaz `set_identity`; v DB se jméno
změní až po potvrzení zařízením (do té doby `identity_pending`). Běží-li na zařízení CAPsMAN
lokality, řízená AP nejdřív dostanou `caps-man-names` se starým i novým jménem (příkaz `cap_names`)
a po přejmenování už jen nové – CAP se drží CAPsMANu podle jména, Wi-Fi tak nevypadne.

### Přehled: proklik z dlaždic a reakce na problémy

Dlaždice přehledu (online, offline, čekající, alerty, aktualizace, problémy verzí, bezpečnost) se
rozkliknou na seznam zařízení s jejich problémy a s akcí k řešení přímo u problému: schválení se
jménem, zálohování, výměna Wi-Fi balíčku, aktualizace, znovu provisioning, potvrzení alertu.
Bezpečnostní nálezy (`GET /tenants/{id}/security`) s přesným vratným příkazem (vypnutí Telnetu, FTP,
HTTP WebFigu, API bez TLS, SOCKS/HTTP proxy, bandwidth serveru – `hardening.FIXES`) jde opravit
jedním klikem: plán `hardening` s commit-confirm (návrat po 5 min bez potvrzení); ostatní nálezy
nabízí ke zkopírování příkazu. www-ssl/ssh/winbox controller nikdy nevypíná.

### Klienti: popisek, ikona, logo; DHCP ze seznamu klientů; klienti v infrastruktuře

| Téma | Implementace |
| --- | --- |
| Popisek, ikona, poznámka | `client_note` (migrace 0017: `label`, `icon`), jen v controlleru; `PUT …/client-notes/{mac}` mění jen poslané klíče |
| Loga | `client_logo` (RLS, checks 24): logo výrobce (klíč = normalizované jméno z OUI, platí pro všechny jeho klienty) nebo klienta (MAC, má přednost); jen PNG/JPEG/WebP do 64 KiB s kontrolou hlavičky souboru, SVG ne. UI obrázek zmenší na 128 px. Výdej s `Content-Security-Policy: default-src 'none'; sandbox`, `nosniff` a soukromou cache |
| Bez loga | ikona z nabídky, jinak odznak s iniciálami výrobce (barva podle jména) – loga značek nedodáváme, nahrává je uživatel |
| Statická IP z Klientů | `GET …/sites/{id}/clients/{mac}/dhcp` najde DHCP server (lease s MAC, jinak podsíť IP klienta) a UI otevře editor rezervace se všemi options klienta |
| Globální DHCP options | sekce inventury `dhcp_networks`; `leases.network_plan` mění na `/ip dhcp-server network` DNS, NTP, doménu a `dhcp-option` pro všechny klienty – plán modulu `dhcp` s návratem; nová option se rovnou založí |
| Infrastruktura | `GET …/infra` vrací u zařízení klienty z posledních 10 min (MAC, popisek, logo); seznam zařízení je rozbalí, strom je s volbou „klienti“ kreslí jako listy (max. 10 na zařízení, zbytek „+N“ do Klientů) |

### Logy ze zařízení a test rychlosti

**Logy (syslog přes tunel)** – služba `mtc-syslog` (`python3 -m mtcontroller.devlog`) poslouchá UDP
`syslog.port` (5514) na `tunnel.controller_ip`. Zařízení IP controlleru nevidí: posílá na tunelovou
adresu koncentrátoru, ten paket dst-natuje na controller a zdroj (tunelová IP zařízení) zůstane –
podle něj se log přiřadí; logy od adres bez zapnutých logů se zahodí. Úroveň po zařízení nebo pro celou
lokalitu: vypnuto / chyby a varování / vše (info bez debug); na zařízení jen `/system logging action`
„mtc“ a pravidla s ní (příkaz `syslog`). Pojistka `syslog.max_lines_per_minute` (600), úklid po
`syslog.retention_days` (14), objem po dnech (`device_log_day`: řádky, bajty, zahozené) – UI ukazuje
průměr za den včetně režie tunelu. Volitelné přeposlání do SIEM (`syslog.forward = "host:port"`).
Tabulky `device_log`, `device_log_day` s RLS (migrace 0018, checks 25).

**Test rychlosti** (`bwtest.py`) – RouterOS bandwidth-test mezi dvěma online zařízeními přes REST v tunelu:
na cíli dočasný uživatel `mtc-btest` (náhodné heslo jen v paměti, povolený jen ze zdrojové adresy podle
`/ip route check`) a bandwidth-server zapnutý jen na dobu testu; po testu (i po chybě) uživatel pryč a server
v původním stavu. Výsledek (průběh po sekundách, průměr/max, CPU obou stran, ztráty) v `bw_test`;
cíl jen na adresu, kterou cíl podle inventury má. UI předem ukáže odhad spotřeby dat.

### Wi-Fi diagnostika klientů: roaming a časová osa

Když si klient stěžuje, rozhoduje **časová osa**: u kterého AP/rádia byl, s jakým signálem, kdy a jak
přešel jinam a kdy a proč se odpojil. Navíc controller z osy sám vyvodí **nálezy s radou** (`wifidiag.diagnose`):
přeskakování mezi dvěma AP (ping-pong), „lepkavý“ klient na slabém AP, opakovaná odpojení s nejčastějším
důvodem, přechody bez FT (plné přihlášení = výpadek), chyby přihlášení (staré heslo), slabý signál, nízká rychlost.

| Zdroj | Co dává | Přesnost |
| --- | --- | --- |
| události z logů (`wireless`, volba „Wi-Fi události“ u zařízení / lokality, migrace 0019 `wifi_event`) | připojení, odpojení s důvodem, roaming (FT), chyby přihlášení | na sekundu |
| vzorky registrační tabulky při inventuře (`wifi_sample`, 7 dní) | AP/rádio, SSID, signál, rychlost | inventura (5 min); změna AP = „odvozený“ přechod |

Rozhraní CAP na CAPsMANu (`cap-wifiN`) se přeloží na AP a rádio podle MAC (`iface_map`), takže je jedno, zda
události posílá CAPsMAN nebo samo AP. UI: Lokalita → Wi-Fi → „Roaming a problémy klientů“ (klienti podle
závažnosti, matice přechodů mezi AP), klik na klienta (nebo Klienti → pravé tlačítko → Wi-Fi časová osa)
otevře plavecké dráhy po AP s barvou podle signálu, značkami přechodů a odpojení a průběhem signálu s hranicí −75 dBm.

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

## Balíčky Wi-Fi a postupná aktualizace od konce sítě

- `packages.py` hlídá Wi-Fi ovladač podle desky a architektury: 802.11ax → `wifi-qcom`, 802.11ac Wave2
  na ARM → `wifi-qcom-ac`, mips/starší → jen `wireless` (v řízeném režimu jen jako switch). Alert
  `wifi_package` (AP nebo deska se zřejmou nápravou) a `package_version` (extra balíček s jinou verzí než
  routeros – RouterOS ho vypne). Příkaz `wifi_package` stáhne správný `.npk` pro verzi a architekturu
  zařízení z download.mikrotik.com, naplánuje odinstalaci špatného a po ack restartuje. Role AP má
  překážku s konkrétní nápravou, dokud ovladač nesedí.
- Postupná aktualizace řadí **od konce sítě**: vzdálenost od gateway v grafu sousedů (MNDP/LLDP),
  nejvzdálenější první, gateway poslední; bez topologie podle role. Ručně: výběr a pořadí z UI
  (`order` v POST), server ověří, že jde jen o kandidáty s dostupnou aktualizací.
  `/system package update install` aktualizuje i extra balíčky (wifi-qcom…) na stejnou verzi.

## Režimy nasazení: vzdálená správa a lokální controller

| | Vzdálená správa (`tunnel.provider = "mikrotik"`) | Lokální (`tunnel.provider = "none"`) |
| --- | --- | --- |
| Kde běží | centrální server, zařízení u zákazníků | v síti, kterou spravuje (Debian, Raspberry Pi, VM) |
| Cesta ke správě | SSTP tunel na koncentrátor, controller src-natovaný na `10.200.0.1` | přímo LAN adresa zařízení |
| `device.tunnel_ip` | adresa z tunelového poolu (IPAM) | adresa, ze které se zařízení hlásí (check-in); mění se s DHCP |
| Firewall na zařízení | z `mtc-sstp` jen správa z koncentrátoru, zbytek z tunelu drop | `chain=input src-address=<controller_ip>` accept (REST, ping), nic se nezahazuje |
| `www-ssl`, účet `mtc-ctl` | jen z `10.200.0.1/32` | jen z `controller_ip/32` |
| Syslog | cíl `10.200.0.1`, koncentrátor dst-natuje | cíl `controller_ip` |
| Failover, routy, alarm `tunnel_down` | ano | ne (koncentrátory nejsou) |

Lokální režim (implementováno):
- `config.TunnelConfig.local`; v lokálním režimu nesmí být koncentrátory ani `manage_routes`,
  `build_providers` vrací `[]`, worker kroky tunelů a rout přeskočí sám (žádní poskytovatelé).
- Adresa správy = zdroj check-inu (`services.set_local_address`, audit `device.address`). Adresu,
  kterou už drží jiné zařízení (NAT, souběh DHCP), nepřebírá. Schválení čeká na první check-in.
- Provisioning (`routeros.provision_body` s `local=True`) odebere případný starý `mtc-sstp`
  (přechod ze vzdálené správy), zbytek (certifikát REST, účet, check-in) je stejný jako u tunelu.
- Převzetí resetem potvrzuje ozvání bootstrapu (`tk`) bez podmínky běžícího tunelu.
- UI: `/api/admin/me` vrací `mode`; v lokálním režimu „IP správy“ místo „Tunel“.

### Instalace jedním balíčkem a průvodce (implementováno, `firstrun.py`, `packaging/`)

Balíček `mtcontroller_<verze>_all.deb` (závislosti jen z Debianu 13: python3-starlette, -uvicorn,
-psycopg, -pydantic, -cryptography, postgresql, nginx). `postinst` založí uživatele, databázi a role,
pustí migrace, vygeneruje hlavní klíč a dočasný certifikát a spustí jen **průvodce** (`mtc-setup`,
uživatel `mtcontroller`, přístup jednorázovým kódem z `/etc/mtcontroller/setup-code`, omezení pokusů).
Průvodce: volba režimu (lokální / centrální), adresa controlleru, hostnamy, sítě správy, certifikát,
první superadmin (MFA při přihlášení), firma a lokalita. Privilegované kroky dělá root
`mtc-setup-apply` (spouští ho `.path` jednotka nad `setup/request.json`): požadavek znovu ověří
(`SetupPlan`), zapíše `config.toml`, certifikáty, nginx a přepne služby; průvodce se vypne.

Vlastní CA (`security.device_ca_file`): RSA 2048, klíč jen root v `/etc/ssl/mtc`, veřejná kopie pro
nginx v `/etc/ssl/mtc-public`. Skripty pro zařízení (`routeros.trust_ca_body`) – adopční one-liner,
ohlášení výchozí konfigurace, bootstrap převzetí – CA stáhnou přes HTTP, a pokud otisk SHA-256
nesedí s hodnotou ve skriptu, vše nově importované smažou. Ověřování TLS na zařízení tak zůstává
zapnuté (`yes-without-crl`) i bez veřejné CA.

### Napojení lokálního controlleru na centrální (uplink, návrh)

Cíl: lokální controller (edge) spravuje své zařízení sám a do centrálu vede **jedno odchozí HTTPS
spojení** – žádné tunely na jednotlivá zařízení, žádné otevřené porty u zákazníka.

1. **Spárování**: v centrálu se pro firmu/lokalitu vystaví jednorázový párovací kód. Edge si
   vygeneruje klíč Ed25519 (`cryptography`), pošle veřejný klíč s kódem; centrál uloží
   `edge_controller` (tenant tabulka s RLS) a vrátí svůj veřejný klíč. Dál jsou všechny zprávy
   podepsané (časové razítko + nonce proti přehrání), TLS s ověřením certifikátu centrálu.
2. **Přenos**: worker na edge drží long-poll `POST /api/uplink/sync` (≈25 s): nahoru posílá
   změny (přehled zařízení, alarmy, zdraví, klienti – shrnutí, ne surová data), dolů dostává
   frontu požadavků. Funguje přes NAT/proxy, ze zákazníkovy sítě jen 443 ven.
3. **Vzdálená správa = relé API**: když technik v centrálu otevře lokalitu edge, UI volá stejné
   `/api/admin/...` endpointy; centrál je nezpracuje sám, ale zařadí jako požadavek pro edge.
   Edge je provede proti vlastnímu API jako servisní principal omezený na spárovanou firmu a
   whitelist cest (bez správy uživatelů, systému a uplinku) a vrátí odpověď. Celé UI tak funguje
   vzdáleně bez replikace všech tabulek; přehled a alarmy z bodu 2 jsou v centrálu vidět i při
   výpadku edge.
4. **Bezpečnost a audit**: edge může cloud kdykoli odpojit (zneplatní klíč); každá akce přes relé
   se zapíše na obou stranách (`audit_log` s `via: uplink` a uživatelem z centrálu); tajemství
   zařízení nikdy neopouštějí edge.

## Zálohy a obnova controlleru

`sysbackup.py`, spouští root (`mtc-admin backup`): podle plánu z administrace (`mtc-backup.timer`
běží co 15 minut, `backupplan.py` rozhodne, zda je čas), na žádost z UI
(Systém → Zálohy zapíše `/var/lib/mtcontroller/backup-request`, `mtc-backup.path` spustí službu).

| Co | Jak |
| --- | --- |
| Obsah | `pg_dump -Fc` celé DB (jako `postgres` – RLS je FORCE i pro vlastníka schématu), `config.toml`, `master.key`, hesla koncentrátorů, CA pro zařízení (`ssl/ca.key`, aby jí zařízení věřila i po obnově), certifikáty nginx a jejich druh (`active.json` – vlastní/koupený certifikát přežije obnovu), plán průvodce, `manifest.json` (formát, verze aplikace, verze schématu) |
| Šifrování | heslo záloh → scrypt (2^15, 8, 1) → AES-256-GCM po blocích 1 MiB; číslo bloku a příznak posledního bloku v AAD (zkrácení i přehození se pozná) |
| Heslo | vygeneruje průvodce, `/etc/mtcontroller/backup.key` (0600 root), ukáže se v záchranných údajích a `mtc-admin recovery-kit` |
| Plán | `system_setting` `backup`: zapnuto, čas (místní čas serveru), dny v týdnu, kolik nechat; zmeškaný termín se dožene týž den, po chybě nový pokus nejdřív za hodinu |
| Uložení | `/var/backups/mtcontroller` (0750 root:mtcontroller, soubory 0640 – UI je nabízí ke stažení, jsou šifrované), ponechá se `keep` z plánu |
| Mimo server | `cloudbackup.py`, tabulka `backup_target` (jen provozovatel, RLS): Nextcloud (WebDAV, heslo aplikace, chunked upload v2 nad 10 MiB), Google Drive (OAuth device code, scope `drive.file`, resumable upload), OneDrive (device code, `Files.ReadWrite.AppFolder`, upload session). Víc cílů, každý vlastní `keep` (maže jen své `mtc-*.mtcbak`). Tajemství v SecretBox (kontext `backup_target:<id>`), refresh token Microsoftu rotuje a ukládá se po každém obnovení. Nahrává worker (uživatel mtcontroller, soubory čte přes skupinu) ve vlákně; po chybě odklad 15 min → 6 h, „Odeslat teď“ v UI |
| Kontrola | `doctor`: heslo existuje, poslední záloha, cíle mimo server; worker `sysalerts.py`: záloha starší než plán + 6 h, chyba zálohy, cíl nepřihlášený nebo 2× selhal, plán vypnutý → notifikace cílům `[[notify]]` bez omezení na tenanty (jednou při otevření, jednou při vyřešení) |
| Audit | `system.backup`, `system.backup_failed`, `system.backup_request`, `system.backup_download`, `system.backup_plan`, `system.backup_target_*`, `system.backup_upload(_failed)`, `system.restore` |

Obnova (průvodce „Obnovit ze zálohy“ nebo `mtc-admin restore SOUBOR`): rozšifrovat, ověřit
manifest a obsah tar (jen očekávané cesty), **zkontrolovat verzi**, znovu založit prázdnou DB,
`pg_restore` jako `postgres`, vrátit soubory a pustit migrace.

Verze: manifest nese číslo posledního schématu. Starší záloha → po `pg_restore` doběhnou migrace
(otestováno: záloha bez poslední migrace se obnoví a domigruje). Záloha z novější verze, než zná
instalovaná aplikace, se odmítne dřív, než se sáhne na DB – nejdřív nainstalovat aspoň stejnou
verzi. Konfigurace je zpětně kompatibilní (nové volby mají výchozí hodnoty). Po obnově na jiném
serveru průvodce upozorní, když se IP serveru liší od `controller_ip` v záloze (zařízení se hlásí
na původní adresu).

Jeden controller = jedna složka v cloudu: retence maže nejstarší `mtc-*.mtcbak` ve složce, dva
controllery ve stejné složce by si zálohy mazaly navzájem. Po obnově na nový server se cíle
obnoví z DB a nový server pokračuje do stejné složky – starý server je potřeba vypnout.

Google OAuth aplikace ve stavu „Testing“ má refresh token jen na 7 dní – je nutné ji přepnout do
produkce (scope `drive.file` nevyžaduje ověření Googlem).

### Průvodce prvního spuštění – kroky

Kód → nová instalace / obnova → režim → síť a certifikát → region (země pro Wi-Fi, časová zóna,
NTP → `[site_defaults]`, `regional.py`; časová zóna i pro server) → správce, firma a lokalita →
shrnutí → průběh → **záchranné údaje** (heslo záloh, DB bez hesel – peer přes unix socket, cesty;
stáhnout / vytisknout, potvrdit uložení) → přepnutí na controller. Obnova vynechá režim, region a
správce (vše je v záloze).

## Nástroje portů, hromadné úpravy, LACP/MLAG, Wi-Fi výkon a podvržená AP

Podle srovnání s UniFi, Meraki, Aruba a Omada (dokument „Správa switchů a AP – co převzít“):

- **Živé nástroje** (`porttools.run_tool`, REST v tunelu, audit `device.tool.*`): ping a traceroute ze
  zařízení, Wake-on-LAN (rozhraní = bridge, kde je MAC vidět), test kabelu (`cable-test` s `duration`,
  na starších `once`), blikání LED portu. Restart portu (`port_bounce`) a PoE (`poe_cycle`) jdou frontou
  příkazů – skript doběhne na zařízení celý i na uplinku.
- **PoE hlídač**: RouterOS umí nativně (`/interface ethernet poe power-cycle-ping-enabled/address/timeout`,
  adresa IP nebo MAC – MAC-ping jen z daného portu). Controller navrhne cíl: IP souseda MNDP/LLDP, IP klienta
  na portu, MAC MikroTiku. Výchozí 60 s (RouterOS 5 s je na restart AP krátké).
- **Profily portů** jako UniFi (PoE, native, tagované, edge, hlídač), úprava profilu, **hromadné použití**
  (`porttools.bulk`): jeden plán s návratem na switch; `portcfg.plan_many` skládá členství ve VLAN přes porty.
- **LACP/MLAG** (`bondcfg`): bond 802.3ad z portů (VLAN a PVID z portů na bond, návrat vrátí porty s jejich
  nastavením); MLAG dvojice – peer port tagovaný ve všech VLAN, `mlag-id` na bondech, RouterOS 7.22+
  `mlag-peer-port`/`mlag-priority` na bridgi, starší `/interface bridge mlag`; kontroly (vlan-filtering,
  L3 HW offload, přímé spojení peer portů, stejná priorita bridge).
- **Zámek portu**: `learn=no`, statický bridge host a ACL čipu (Marvell Prestera – CRS3xx/5xx; povolit MAC,
  zbytek zahodit), jinde bridge filtr (jen provoz přes CPU – varování). Komentář `mtc-lock <port>`.
- **Wi-Fi**: `txpower.py` (vyvážený výkon), přepínač 802.11k/v, sken okolí (`rogue.py`, tabulka `wifi_bss`
  s RLS, migrace 0020): own / rogue (cizí BSSID s naším SSID → alert `rogue_ap`) / neighbor; noční sken
  2–5 h podle politiky `rogue_scan: night`.

K ověření v labu: `cable-test` a `blink` přes REST, ACL pravidla zámku na CRS3xx, MLAG na dvou CRS
(7.22+ i 7.16), výstup `/interface/wifi/scan` přes REST (pole `address`, `channel`, `signal`).

## Certifikáty HTTPS (tlscfg)

Instalace nesmí záviset na Let's Encrypt. nginx ukazuje na stálé sloty `/etc/ssl/mtc/admin.{crt,key}`
a `checkin.{crt,key}` (lokální režim jen `admin`); obsah slotů se mění bez úpravy konfigurace nginx.

- **Start:** průvodce, `install.sh` i aktualizace balíčku (`mtc-admin tls bootstrap`) nejdřív naplní
  prázdné sloty certifikátem z CA controlleru (jména + IP controlleru) – nginx naběhne vždy.
  Starší instalace s LE `mtcontroller` se převezme. `device_ca_file` je v konfiguraci vždy: CA
  controlleru zařízení dostanou při adopci a návrat check-inu na ni funguje bez zásahu na zařízení.
- **Let's Encrypt:** `certbot certonly --webroot` (port 80 obsluhuje nginx vždy, i průvodce),
  lineage `mtc-<slot>`. Selhání = varování, zůstane předchozí certifikát. Obnovy přenese do slotu
  deploy hook (`renewal-hooks/deploy/mtcontroller` → `mtc-admin tls-sync`).
- **Vlastní PEM:** kontrola, že klíč patří k certifikátu a platí; jiné jméno jen varování.
- **Návrat:** po každé změně `nginx -t`; při chybě se vrátí předchozí soubory slotů.
- **Oprávnění:** web (superadmin) jen ověří vstup a zapíše `tls-request.json` (0600); provádí root
  `mtc-tls.path` → `mtc-admin tls-apply`, stav do `tls-status.json`, audit `system.tls*`
  (bez klíče). Veřejné kopie certifikátů a `active.json` (druh slotu) jsou v `/etc/ssl/mtc-public`.
- **Zařízení:** check-in ověřují; veřejnou CA jen s RouterOS 7.19+ a vestavěnými CA pro fetch (`/certificate/settings builtin-trust-store`).
  UI proto u check-inu varuje a doporučuje LE jen pro administraci.
- `mtc-admin doctor` hlásí chybějící slot, nepokryté jméno a expiraci < 14 dní.

## Rozhraní jako v UniFi (etapa 1 zjednodušení)

Navigace je vázaná na lokalitu (výběr nahoře v levém panelu) a má pět sekcí:

| Sekce | Obsah |
|---|---|
| Přehled a topologie | dlaždice stavu lokality (proklik s opravou), mapa infrastruktury, topologie, STP |
| Zařízení | tabulka zařízení lokality (čekající bez lokality ve všech); „Porty všech switchů“ |
| Klienti | drátoví i Wi-Fi klienti; panel klienta |
| Nastavení lokality | Sítě (VLAN, DHCP, leases, DHCP snooping) · Wi-Fi (SSID, skupiny AP, pokročilé nastavení rádií sbalené, roaming, rádia) · Profily portů · Systém a nasazení (adopce, NTP, postupná aktualizace, výchozí konfigurace) |
| Aktivita a audit | alerty, změny (plány – žurnál a schvalování), audit |

Pod čarou: Všechny lokality (seznam, hledání klienta v tenantovi), Uživatelé, Systém (superadmin).

- **Žádná podstránka detailu**: klik na zařízení (tabulka, mapa, topologie) otevře panel vpravo se
  záložkami Přehled (porty, připojení, akce, souhrn, sbalené Řízení zařízení s pokročilými moduly a
  Zálohy a historie), Porty (správce portů, provoz, Time Machine), Wi-Fi (jen AP: rádia, SSID,
  skupina) a Diagnostika a logy (bezpečnost a firewall, VLAN a rozhraní, nástroje a test
  rychlosti, logy). Esc panel zavře.
- **Panel klienta** nahradil dialogy: popisek, ikona, logo, **pevná IP na jeden klik** (rezervace
  aktuální adresy na DHCP serveru, který klienta obsluhuje – plán dhcp s automatickým návratem;
  bez práva správce se uloží ke schválení) a Wi-Fi časová osa jako záložka.
- Podzáložky si prohlížeč pamatuje (`prefs.sub`); po změně lokality se otevřená sekce načte znovu.
- Etapa 2 (backend) přinese automatické nasazení změn záměru a řetězec adopce; potom zmizí
  tlačítka „Připravit VLAN lokality“, „Sladit řízená zařízení“ a moduly observe/manage z panelu.

## Uložit = nasadit (etapa 2a)

`autoapply.py`: po uložení sítí lokality, SSID, skupin AP, výjimek zařízení nebo portu řízeného
switche se pro **řízená** zařízení spočítá rozdíl (`switchcfg.reconcile`, CAPsMAN), plán se hned
schválí pod jménem uživatele a zařízení se probudí (kick). Commit-confirm s návratem do 5 minut
platí dál, plány zůstávají jako žurnál (Aktivita → Změny). Odpověď API nese `apply`
(`applying` / `draft` / `blocked` / `ok` po zařízeních); UI ukáže neblokující ukazatel
„Aplikuji změny… → Hotovo“ podle `GET /tenants/{tid}/plans/status?ids=`.

- Ke schválení zůstane plán u uživatele bez práva schvalovat (technik) a u modulů ve
  `security.four_eyes_modules` (výchozí jen `firewall`; kdo přidá `switch`/`capsman`, má čtyři oči i na sítě).
- CAPsMAN se plánuje jen při změně skriptu proti poslednímu nasazenému.
- **Porty řízeného switche**: VLAN část (PVID, tagované, vypnutí) se uloží jako výjimka záměru
  zařízení a sladí; PoE, popis, edge a hlídač jdou plánem portu. Všechny sítě lokality tagovaně
  = `trunk`, bez tagovaných = `access`, jinak nový režim `hybrid` (native VLAN + vybrané tagované).
  Změna, kterou model odmítne (uplink jako access), se neuloží.
- Řízené zařízení má spravovatelné moduly vždy `manage` (přepínače observe/manage jen u sledovaných);
  LACP a „Připravit VLAN lokality“ jen u sledovaných (porty řízeného určuje záměr).

## Adopce na jeden krok (etapa 2b)

Dialog adopce má jméno, roli (**Switch** / **AP + switch** / **Jen sledovat** – výchozí podle modelu:
cAP/wAP/hAP… → AP + switch, když lokalita má sítě; CCR/RB5009/hEX… → jen sledovat) a skupinu AP.
`POST …/devices/{id}/approve` s `mode` spustí řetězec v `device.adopt_flow` (migrace 0023), který
posouvá worker (`adoptflow.run_all`, krok `adopt`):

`provision` → `inventory` (čerstvá po schválení) → `wifi_pkg` (AP se špatným ovladačem: `wifi_package`,
restart, nová inventura) → `takeover` → `takeover_wait` → `wifi` → `done`.

- **factory / simple** → převzetí načisto bez ptaní (plán `takeover`, záloha, reset, návrat ze zálohy
  při výpadku), schválené pod uživatelem, který adopci schválil.
- **conforming** (VLAN na jednom bridgi `bridge`) → řízené hned bez resetu, rozdíl doladí sladění.
- **custom** → `needs_confirm`: u zařízení banner „Přepsat načisto“ / „Ponechat ke sledování“
  (`POST …/devices/{id}/adopt` s `action` overwrite / monitor / retry).
- Čekání s vysvětlením v `msg` (lokalita bez sítí, AP bez řízeného switche s CAPsMANem) se zkouší znovu;
  CAPsMAN lokality se vybere sám (první řízený switch s nejvíc porty), když ho lokalita nemá a má SSID.
- Krok `wifi` nasadí Wi-Fi lokality (autoapply), takže nové AP vysílá bez dalšího klikání.
- Sledované zařízení jde převzít stejným řetězcem (`action: manage`, tlačítka „Řídit jako switch /
  AP + switch“ v Přehledu). Bez práva schvalovat zůstane plán převzetí ke schválení.

## Čas a DNS řízených zařízení (etapa 3a)

- Controller je NTP server (chrony z Debianu, závislost balíčku). `mtc-admin netsvc-apply` (root,
  `mtc-netsvc.path` na žádost z UI, postinst, průvodce) zapíše `/etc/chrony/sources.d/mtcontroller.sources`
  (zdroje času controlleru ze Systém → Čas a DNS, výchozí regionální) a `/etc/chrony/conf.d/mtcontroller.conf`
  (`allow`: lokální režim privátní sítě, vzdálený tunelový pool) a vypne výchozí `pool` v chrony.conf
  (originál `chrony.conf.mtc-orig`). Stav pro UI `netsvc-status.json`; `mtc-admin doctor` hlásí chybějící konfiguraci.
- Řízené switche a AP mají NTP vždy `tunnel.controller_ip` a DNS z administrace (prázdné = z DHCP).
  Model nese `ntp_ctl` a `dns_servers`; převzetí je nastaví, sladění je drží (s návratem) a otisk
  sladění zahrnuje NTP i DNS. NTP politika lokality platí jen pro sledovaná zařízení s modulem NTP.
- Nastavení v `system_setting` (klíč `netsvc`, jen provozovatel), změna se audituje (`system.netsvc`).

## Izolace řízených zařízení a balíčky přes controller (etapa 3b, 3c)

- **Izolace (jen lokální režim)**: řízené switche a AP mají v `chain=output` pravidla `mtc isolate` –
  vlastní provoz zařízení jen na lokální/broadcast/multicast adresy, privátní sítě (RFC 1918, 100.64/10,
  169.254/16), controller a DNS z administrace; zbytek drop (IPv6: jen link-local, multicast, ULA).
  Provoz klientů přes bridge chain output neprochází. Převzetí pravidla nasadí, sladění je drží.
  Ve vzdáleném režimu se neizoluje (zařízení potřebují internet k SSTP koncentrátoru).
- **Výjimka** „Povolit přístup do internetu“ (`device_intent.internet`, migrace 0024,
  `PUT …/devices/{id}/internet`, správce, audit `device.internet`) pravidla odebere sladěním.
- **Balíčky přes controller** (`pkgmirror.py`): pro izolovaná zařízení stahuje worker (krok `pkgmirror`)
  balíčky z download.mikrotik.com do `<data_dir>/pkg` (staré po 45 dnech pryč), zařízení je bere z
  `/api/checkin/pkg/<soubor>` s klíčem check-inu. Příkaz `upgrade` / `wifi_package` čeká ve frontě,
  dokud balíčky nejsou u controlleru. Aktualizace = stažení všech zapnutých balíčků a restart.
  Dostupnou verzi kanálu zjišťuje controller (upgrade.mikrotik.com `NEWESTa7.<kanál>`, každých 6 h) a doplní
  ji do inventury izolovaného zařízení; zbytek (schválení verze, postupná aktualizace) funguje beze změny.
  x86/CHR zatím jen s výjimkou.

## Úklid rozhraní, snazší adopce, design (etapa 4)

- **Přidat zařízení** (tlačítko v Zařízeních, první kroky prázdné lokality): jeden dialog – vygeneruje příkaz,
  čeká na ohlášení a nabídne rovnou adopci. Pod seznamem zařízení „Nalezeno v síti“: nespravované MikroTiky,
  které řízená zařízení vidí jako sousedy (MNDP, `platform`).
- **Automatická adopce** (`site.auto_adopt`, migrace 0025, výchozí vypnuto, `PUT …/sites/{id}/auto-adopt`, správce,
  audit): zařízení ohlášené do lokality se schválí samo s rolí podle modelu (`adoptflow.suggest_target`). Řetězec má
  příznak `auto` – bez ptaní převezme jen tovární (a vyhovující) konfiguraci, jednoduchou i vlastní zastaví na
  rozhodnutí člověka.
- **Úklid**: zakládání tenanta ve Všech lokalitách; upozornění a novinky verze v panelu zařízení sbalené do jednoho
  řádku; souhrn bez duplicit; převzetí do řízení se nenabízí u bran; stavy a role česky; „Nasadit“ u DHCP snoopingu
  a NTP sledovaných zařízení plány rovnou schválí (technikovi zůstanou ke schválení); bridge + DHCP snooping,
  NTP sledovaných a příprava před montáží (výchozí konfigurace, Netinstall) jsou sbalené jako pokročilé.
- **Design**: písmo 12 / 14 / 16 / 20 px (základ 14; drobné popisky jen v grafice), výchozí tlačítko neutrální a
  plné jen hlavní (`.primary`, odeslání formuláře), navigace s ikonami, první kroky v prázdné lokalitě.

## LACP na řízených zařízeních a optimalizace kanálů (etapa 5)

- **LACP v záměru zařízení** (`device_intent.bonds`, migrace 0026): `[{name, ports, mode trunk|access|hybrid, vlan,
  tagged, lacp_rate, hash}]`. Členy jsou libovolné porty (nemusí sousedit), 2–8, každý nejvýš v jednom bondu; člen
  nemá vlastní výjimku portu. `switchcfg.build` členy vyjme z portů bridge a přidá bond jako jeden port (položka
  s klíčem `bond`); bond s uplinkem je vždy trunk a `trusted`. `mgmt_path` pozná uplink i přes bond (první člen).
- **Sladění** (`switchcfg.reconcile`): bondy controlleru (komentář `mtc`) vznikají a mizí před VLAN tabulkou –
  nové členy z bridge ven → `bonding add` → VLAN tabulka → port bondu do bridge; zrušení opačně. Návrat má pevné
  pořadí: ostatní kroky pozpátku, obnova zrušených bondů, VLAN tabulka, zrušení nových bondů a členy zpět do bridge.
  Převzetí načisto (`config_body`) bond vytvoří před jeho portem.
- **API**: `POST …/devices/{id}/bonds` u řízeného zařízení zapíše bond do záměru (`autoapply.bond`, audit
  `device.intent`) a rovnou sladí; VLAN bondu se bere z prvního portu, profilu nebo ručně a dál se mění editorem
  portu (jde do záznamu bondu). U sledovaných zařízení zůstává plán `bondcfg`.
- **Optimalizace kanálů** (`rfopt.py`): každý sken AP (`rogue.store`) uloží, co AP slyší, do `site_policy`
  modul `wifi_scan` (nejvýš 80 sítí na AP, platnost 7 dní). `rfopt.plan` počítá rušení kanálu = cizí sítě vážené
  signálem a překryvem + vlastní AP, která se slyší (dvojice bez měření se počítá odhadem). Hledá kanál a v 5 GHz
  i šířku s nejmenším rušením (zúžení má cenu, strop šířky se zkouší pro celé pásmo), stávající kanál má přednost,
  není-li znatelně horší. Volitelně sníží výkon rádia, které na stejném kanálu slyší vlastní AP silněji než
  −70 dBm (1 dB za 3 dB, nejvýš 6 dB, ne když jsou klienti daleko).
- **Nasazení**: `GET/POST …/sites/{id}/wifi/optimize` (náhled / připnutí / `action: reset`). Připnutí
  (`site_policy` modul `wifi_rf`, audit `wifi.optimize`) má v `wificfg.manager` přednost před `channels.plan`
  a `txpower.plan`, dokud vyhovuje politice (šířka, DFS); nasazuje se přes CAPsMAN jako každá změna Wi-Fi.
  UI: Nastavení → Wi-Fi → „Optimalizovat kanály“ (měření AP po jednom, náhled před/po, nasazení).

## Bezpečnější změny a vzhled (etapa 6)

- **Předpověď odříznutí** (`portcfg.lockout_risk`): změna portu sledovaného zařízení se před vytvořením plánu
  porovná s cestou ke správě (`switchcfg.mgmt_path` – uplink a zda je správa na něm tagovaná). Vypnutí uplinku,
  změna jeho PVID u netagované správy, PVID = VLAN správy a odebrání VLAN správy z uplinku se odmítnou
  (`LockoutError`); správce je může vynutit (`desired.force`), commit-confirm zůstává pojistkou. U řízených
  zařízení totéž hlídají překážky modelu (`switchcfg.build`).
- **Kanárek při postupné aktualizaci** (`upgrades`): první zařízení úlohy má `canary` minut (API výchozí 10).
  Po úspěšné aktualizaci přejde do stavu `soak`; když během sledování vypadne, hlásí jinou verzi, přestane se
  hlásit nebo se u něj otevře kritický alert, úloha se zastaví a zbytek lokality zůstane beze změny.
- **Servisní okna** (`maintenance.py`, `site_policy` modul `maintenance`): během okna `notify.deliver` neposílá
  nové alerty z lokality; co po skončení trvá, odejde, co mezitím vzniklo i zaniklo, neodejde vůbec. Za okno se
  počítá i běžící postupná aktualizace. API `GET/POST …/sites/{id}/maintenance`, `DELETE …/maintenance/{id}`,
  audit `site.maintenance_add/_end`. UI: Aktivita → Alerty → „Servisní okno…“.
- **Vzhled podle UniFi Network**: úzká ikonová lišta (76 px, popisek pod ikonou, celý název v tooltipu), výběr
  lokality v horní liště vedle tenanta, dlaždice přehledu se stavovou tečkou, klidnější hlavičky tabulek, modrá
  `#006fff`. Vše jako blok na konci `style.css` (původní pravidla zůstávají pod ním).

## Klienti v čase, topologie jako strom, QR pro MFA (etapa 7)

- **Relace klientů** (`client_session`, migrace 0027): worker při `clients.record` (co 5 min) přítomným klientům
  otevře / potvrdí relaci (`seen_at`) a ostatním ji ukončí. Relace nepotvrzená déle než 20 min (worker stál,
  lokalita offline) končí posledním potvrzením. Retence 90 dní spolu s historií klientů.
- **Posun v čase**: `clients.timeline` (počty celkem / Wi-Fi po 5 min až 4 h podle období) a `clients.at`
  (kdo byl připojený v daný okamžik). API `GET …/sites/{id}/clients/timeline?hours=` a `…/clients?at=ISO`.
  UI Klienti: graf s posuvníkem (24 h / 7 dní / 30 dní), přepínač Vše / Wi-Fi / Kabel, hledání, filtr sítě
  (VLAN, SSID) a zařízení, sloupec Připojení a síla signálu.
- **Topologie**: strom zleva doprava (kostra BFS od řízeného zařízení s nejvíc spoji). Každý list má vlastní
  řádek, popisek je v rámečku, porty spoje nad vodorovným úsekem u potomka; spoje mimo kostru čárkovaným
  obloukem, nespojené části pod sebou. Plátno má pevné rozměry a posouvá se, neškáluje.
- **QR pro MFA** (`qr.py`): vlastní kodér QR (bajtový režim, úroveň M, verze 1–10, výběr masky) bez závislostí;
  `auth` vrací k otpauth URI matici `qr`, UI ji vykreslí jako SVG. Ruční klíč zůstává jako záloha.

## RouterBOOT s RouterOS, automatické a naplánované aktualizace (etapa 8)

- **RouterBOOT hned po RouterOS** (`services._firmware_follow`): když zařízení po aktualizaci vyžádané
  controllerem (příkaz `upgrade` hotový v posledních 6 h) ohlásí v check-inu novou verzi, zařadí se mu příkaz
  `firmware`. Ten si na zařízení sám ověří `current-firmware` ≠ `upgrade-firmware` a jen tehdy restartuje –
  není potřeba čekat na inventuru. Ruční aktualizace mimo controller se nedotahuje (žádný nečekaný restart).
- **Postupná úloha** (`upgrades`): krok RouterOS je hotový až po doběhnutí navazujícího `firmware` a check-inu
  aspoň 75 s po jeho potvrzení (příkaz potvrzuje před restartem). Kandidáti zahrnují i zařízení s aktuálním
  RouterOS a starším RouterBOOT – krok `kind: firmware` pošle jen příkaz `firmware`.
- **Automatika a plánování** (`autoupdate.py`, `site_policy` modul `autoupdate`): v okně (dny + čas, místní čas
  serveru) worker spustí postupnou úlohu pro zařízení, jejichž verze je nabízená aspoň `delay_days` dní
  (`seen` = kdy byla verze poprvé viděna), nejvýš jednou za 20 h. `scheduled {at, order}` = jednorázový start,
  volitelně jen vybraná zařízení; termín opožděný o víc než 6 h propadá. API `PUT …/sites/{id}/autoupdate`,
  stav v `GET …/upgrade` (`auto`), audit `site.autoupdate`, `site.autoupdate_run`.

## Překlady rozhraní (etapa 9)

- **Zdrojový jazyk je čeština** a český text je zároveň klíč (jako gettext). V JS jdou texty přes `__("…")`
  a tag `` __`… ${x}` `` (klíč má místo hodnot `{0}`, `{1}`); statické HTML se překládá při načtení po
  textových uzlech a atributech `title` / `placeholder` / `aria-label` / `alt`.
- **Zavaděč** `static/i18n.js`: jazyk z `localStorage mtc-lang`, jinak podle prohlížeče (cs/sk → čeština, jinak
  angličtina). Pro jiný jazyk než češtinu stáhne `/i18n/<kód>.json`, přeloží DOM a teprve pak spustí `app.js`
  (`window.MTC_DICT`, `MTC_LOCALE` pro formát dat). Chybějící slovník nebo text → čeština.
- **Slovníky**: `src/mtcontroller/i18n/<kód>.json` (`language`, `code`, `locale`, `strings`), `template.json` je
  prázdná šablona. Vlastní a upravené slovníky v `<data_dir>/i18n/` mají přednost text po textu a přežijí
  upgrade. `GET /i18n/index.json` (seznam jazyků) a `/i18n/<kód>.json` jsou veřejné – potřebuje je i přihlášení.
- **Nástroje**: `tools/i18n.py template | check <kód> | sync <kód>`; test `test_i18n.py` hlídá úplnost
  angličtiny, shodu zástupných znaků a mezer na krajích. `tools/i18n_wrap.py` je jednorázový převod starého kódu.
- **Zatím nepřeloženo**: průvodce instalací (`setup.html/js`) a texty generované serverem (chyby API, alerty,
  notifikace, audit). Návod pro překladatele: `docs/TRANSLATING.md`.

## Řízená brána – gateway mode, krok 1 (etapa 10)

Brána je řízené zařízení jako switch: stejné převzetí (záloha → reset → bootstrap → návrat ze zálohy,
když controller do 12 min nepotvrdí), stejný bridge s VLAN filtrováním, stejné sladění s commit-confirm.
Navíc má model část `gw` (`gwcfg.py`):

- **WAN**: port mimo bridge (zadaný, jinak port dnešní výchozí routy); DHCP / statická adresa / PPPoE,
  volitelně ve VLAN poskytovatele (`wan-vlan`, `pppoe-wan`). Heslo PPPoE je tajemství zařízení
  (`secret_kind` `wan_password`, migrace 0028) – model ani plán ho nenesou, do bootstrapu se doplní až při
  vydání zařízení; v náhledu je maska.
- **Sítě lokality**: každá síť s podsítí dostane na bráně adresu (síť managementu na `bridge`, ostatní na
  `vlan<id>` nad bridgem – bridge je tagovaný člen VLAN), DHCP server `mtc-v<id>` (rozsah a zápůjčka jdou
  přepsat) a DNS (brána sama, nebo DNS sítě). Síť bez podsítě zůstává jen L2.
- **Zóny** podle účelu sítě: mgmt→MGMT, lan→LAN, hoste→GUEST, iot→IOT, kamery→CAM, voip→VOIP, jine→OTHER,
  plus WAN. Výchozí politiky: každá zóna do internetu, MGMT a LAN do všech vnitřních sítí, ostatní mezi sebou
  ne; správa routeru (`mtc-admin`) jen ze sítí MGMT (jinak LAN). Pravidla rendruje `fwmatrix` (komentáře
  `mtc-fw: …`), maškaráda na WAN. Pravidla správy controllerem (`mtc: …`) vznikají až po firewallu a přesunou
  se nad něj.
- **Nastavení brány** je v `site_policy` modul `gateway` (`{device_id, wan, dhcp}`), jedna brána na lokalitu;
  API `GET/PUT …/devices/{id}/gateway`. UI: detail zařízení → záložka Řízení.
- **Sladění** (`gwcfg.reconcile`, voláno ze `switchcfg.reconcile`): přidání/zrušení/změna sítě (rozhraní,
  adresa, DHCP), členství v zónách, nová zóna (služby routeru, provoz uvnitř zóny), politiky mezi zónami
  a seznam `mtc-admin` (sekce inventury `fw_admin`). Zrušené DHCP servery se odebírají dřív, než vzniknou
  nové (na rozhraní smí být jeden). Brána z výchozí konfigurace role firewall (`mtc-lan`, 192.168.88.0/24)
  se sladěním převede na sítě lokality – ale až po uložení nastavení brány; bez něj se nesladí.
- **Zatím neumí**: změnu WAN za provozu (jen při převzetí), úpravu matice zón v UI (krok 2), druhou linku
  (krok 3), port-forwardy, IPv6 prefixy, VPN. Neověřeno na skutečném hardwaru.

Rozhraní: detail zařízení má záložku **Řízení** (obecné údaje, převzetí, výjimky, brána) místo sbaleného
bloku v Přehledu; nastavení lokality má sekce vlevo pod sebou; v záhlaví je rychlé hledání (Ctrl+K) přes
sekce i zařízení.

## Časová osa lokality (etapa 11)

`timeline.py` skládá na jednu osu události, které controller už sbírá (nic nového se neukládá): alerty
(vznik / pominutí), `port_event` (linka, rychlost, PoE, soused, smyčka), `client_session` (připojení /
odpojení klienta), `wifi_event` (roaming, neúspěšné přihlášení), `config_plan` (provedeno / selhalo /
vráceno) a zásahy člověka z auditu (cíl `site:` / `device:` lokality; bez přihlášení a plánů).
API `GET …/sites/{id}/timeline?hours=|minutes=&until=&cats=&device=&mac=` vrací události od nejnovější
(nejvýš 500) a 48 úseků pro graf (počet + nejhorší závažnost) počítaných ze všech událostí okna.

UI: Aktivita → Časová osa (výchozí podzáložka). Graf nad seznamem – klik na sloupec přiblíží úsek;
filtry kategorie, zařízení, klient, hledání v textu; u každé události „okolí ±5 min“ = všechno, co se
dělo kolem, napříč zařízeními a klienty (hledání příčiny). Vstup i z detailu zařízení a klienta
(tlačítko Časová osa). Texty událostí portů a alertů generuje server (zatím jen česky), akce auditu se
zobrazují kódem.

## Sousedé hlášení na VLAN rozhraní (etapa 12)

Při VLAN filtrování na bridgi přijde MNDP/LLDP přes rozhraní VLAN a RouterOS pak u souseda neuvede port,
ale jméno VLAN rozhraní (`93-bridge`, `vlan10`). Všechna zařízení téže VLAN tak vypadala jako sousedé na
jednom „portu“ – mapa z toho udělala hvězdu nebo „nespravovaný switch“ a popisky nesly jméno VLAN.
`inventory.resolve_neighbor_ports` proto port dohledá: podle MAC souseda v tabulce bridge hostů
(`on-interface`), jinak podle jiného záznamu téhož souseda, a zapíše `<port>,<původní rozhraní>` (tvar,
který RouterOS sám používá – `ether3,bridge`). Běží při sběru inventury i při čtení (`inventory.load`),
takže se opraví i dříve uložená data; duplicitní záznamy téhož souseda na témž portu se sloučí. Soused,
jehož MAC v tabulce hostů není, zůstane, jak byl.

Mapa lokality navíc skládá strom z tabulek MAC adres bridge (FDB, sekce `bridge_hosts`): pro každé
zařízení port, na kterém vidí MAC ostatních spravovaných zařízení (všechny jejich MAC, ne jen tu ze
sousedů). Od brány: uplink zařízení = port, kde vidí bránu; rodič = zařízení, které ho vidí na jiném než
svém uplink portu a má za tím portem nejméně zařízení. Dá správně kaskádu (switch → spoj → switch → AP)
i bez discovery a s neúplnými tabulkami; porty jsou známé na obou koncích spoje. Jméno VLAN rozhraní nebo
bridge se jako port neukáže (port „?“). Bez brány v lokalitě zůstává původní párové porovnání sousedů.

## Místo na disku a pořadí návratu při převzetí (etapa 13)

Na 16MB flash bývá volných 1–3 MB a RouterOS při plném disku zapíše uříznutý soubor bez chyby. Proto:
controller převzetí nepustí, když inventura hlásí méně než 1 MiB volného místa (`switchcfg.space_blocker`);
zařízení si před zálohou ověří 512 KiB a po zapsání zálohy i bootstrapu musí zbýt 64 KiB, jinak oba soubory
smaže a reset se nespustí. Stejná dvojí kontrola je u balíčků stahovaných z controlleru (`pkgmirror`,
velikost balíčků + 256 KiB předem, 64 KiB po stažení). Aktualizace přes `/system package update` se
neblokuje – tam si místo řeší RouterOS sám.

Bootstrap po resetu zakládá `mtc-revert`, `mtc-confirm-tick` a časovač `mtc-confirm` úplně prvními příkazy,
mimo hlavní blok `{ }` a bez lokálních proměnných – dřív než čekání na porty. Zaseknutý, chybný nebo
uříznutý zbytek skriptu tak návrat ze zálohy nezastaví.

## Dostupnost a odchylky od normálu (etapa 14)

`baseline.py`. **Dostupnost** zařízení se počítá z alertů `offline` (vznik–pominutí), okno nejdřív od
adopce; API `GET …/sites/{id}/availability?days=7|30|90` vrací procenta, počet a délku výpadků a hodnotu
pro každý den. UI: Aktivita → Dostupnost (proužek dnů, klik = časová osa zařízení).

**Normál zařízení**: worker (`baseline.sample`) započítává každou čerstvou inventuru do `device_hourly`
(CPU, paměť; migrace 0029, RLS). `baseline.anomalies` porovná poslední dokončenou hodinu se stejnou hodinou
dne za 14 dní (CPU, paměť z `device_hourly`, provoz nejvytíženějšího rozhraní z `interface_hourly`) a při
výrazné odchylce (3 směrodatné odchylky a zároveň absolutní práh) otevře alert `anomaly` (varování, předmět
= `cpu` / `memory` / `traffic` / `traffic_low`). Potřebuje aspoň 5 dní historie; alert sám pomine, jakmile
další hodina vyjde normálně. Hodiny se porovnávají v UTC (stejná hodina dne je stejná i v místním čase,
kromě týdne se změnou času).

## Vzhled 2 – podle UniFi Network (etapa 15, krok 1)

Směr schválený nad maketami: úzký pruh ikon bez popisků (52 px, popisek v `title`), tenké ploché záhlaví,
bílá plocha bez karet a stínů, husté tabulky (řádek 34 px, písmo 13 px), jedna modrá jako akcent, akce jako
modré odkazy. Druhá úroveň navigace je **skrývací levý panel** (`.ux`): nahoře přepínač pohledů sekce, pod
ním hledání a skupiny filtrů, dole odkazy s akcemi; obsah (tabulka) vpravo přes celou výšku. Panel se
skrývá úchytem na své hraně (`.ux-toggle`, volba se pamatuje pro každou sekci). Technicky: karta sekce je
mřížka `[panel | obsah]`, podzáložky (`.pane`) mají `display: contents`, takže jejich `.ux-side` a
`.ux-main` padnou do sloupců mřížky bez změny přepínání podzáložek. Hotovo: kostra, Zařízení (Seznam,
Porty), Klienti. Zbývá: Přehled/Mapa s panelem zařízení, Události, Nastavení, úzký panel detailu.

Krok 2 (boční panely a časová osa u mapy): obecné **skupiny filtrů** (`facetGroups` v `app.js`) –
zaškrtávátka s počty, počty respektují ostatní skupiny, skupinu jde sbalit (pamatuje se), dlouhé seznamy
mají „dalších N…“. Zařízení: typ, správa, k řešení (upozornění / aktualizace / bezpečnostní nález),
RouterOS, model, lokalita; přepínač **Seřadit podle topologie** (rodič, pod ním potomci, odsazení podle
hloubky – z `uplinks`). Klienti: stav, připojeno k, Wi-Fi síť, VLAN, výrobce. **Časová osa vedle mapy**
(`#infra-tl`): události lokality z `timeline` API na svislé ose (nahoře teď, 6 h / 24 h / 7 d), shluknuté
značky podle závažnosti; klik zvýrazní dotčená zařízení v mapě a nabídne „co se dělo kolem“ v Aktivitě.
Stav mapy v minulosti nevrací – topologie se neukládá v čase.

## Sekce Porty (etapa 15, krok 3)

Samostatná položka levé lišty (`view-ports`, stav `pm` v `app.js`) – přehled portů celé lokality po vzoru
UniFi Ports. Jen čtení; hromadná úprava portů a MLAG zůstávají v Zařízení → Porty všech switchů.

- **Data:** `GET …/sites/{id}/ports` (`porttools.site_ports`) vrací u portu navíc `rate`, `poe_status`, `poe_w`,
  `stp_role`, `sfp`, `peer` (spravovaný soused `{device_id, device, port}` – protější port z `interface-name`
  souseda) a `clients` (drátoví klienti viděni na portu za posledních 20 minut); v odpovědi je i seznam
  `networks` lokality pro jména VLAN.
- **Levý panel:** výběr zařízení (nebo všechny porty), přepínače Schéma portů / Připojená zařízení / VLAN /
  Provoz (ukládají se do `prefs.pm`) a skupiny filtrů (`PM_FACETS`: stav, rychlost linky, PoE, co je připojeno,
  nativní VLAN, STP, profil).
- **Schéma:** dlaždice portů ve dvou řadách (jako na čelním panelu MikroTiku: sudé nahoře, liché dole), SFP zvlášť; barva podle rychlosti linky, značky
  PoE / uplink (STP root port) / bond, nad a pod dlaždicí jméno toho, co je na portu. Porty mimo filtr zešednou,
  klik port vybere (tabulka i graf se zúží na něj).
- **VLAN:** nativní VLAN každého portu a pro každou síť mřížka tagovaná / nativní / není na portu.
- **Provoz:** graf rx/tx za 24 h z `/devices/{id}/traffic` – součet fyzických portů zařízení, nebo vybraný port.

## Zdraví ethernetových portů (etapa 16, krok 1)

Modul `porthealth.py`, tabulka `port_error_hourly` (migrace 0030, RLS, kontrola 20d).

- **Sběr:** inventura čte navíc `rx-packet`/`tx-packet` z `/interface` a sekci `ethernet_stats`
  (`/interface/ethernet` s čítači rx-fcs-error, rx-align-error, rx-fragment, rx-jabber, rx-too-short/long,
  rx-overflow, tx-late-collision, tx-collision, rx/tx-pause…). Sekce je zvlášť od `ethernet`, aby měnící se
  čísla nehýbala otiskem konfigurace; její selhání není chyba inventury. Které čítače čip vede, se liší –
  chybějící se nepočítají a zůstává souhrn rx-error + tx-error (`generic`).
- **Přírůstky:** čítače RouterOS jen rostou, proto `porthealth.record` při uložení inventury odečte předchozí
  stav a rozdíl přičte do hodinového řádku. Pokles čítače (restart) port v tom kroku přeskočí; předchozí stav
  starší než 2 h se nepoužije. Do řádku jde i poslední optický výkon SFP.
- **Hodnocení (`evaluate`):** poměr chyb k rámcům za poslední hodinu. Varování od 20 chyb a 0,01 % rámců,
  vážné od 100 chyb a 1 %; bez počtu rámců absolutně od 100 chyb. Dominantní druh chyby určuje nápovědu
  (FCS = kabel/konektor/rušení, pozdní kolize = duplex, přetečení = přetížení…). Běžné kolize, pause rámce
  a zahozené pakety se evidují, ale chybou nejsou.
- **Alerty:** `port_errors` (nově poměrem a s druhem chyby, dřív absolutně ≥ 100/h z `interface_hourly`)
  a `sfp_power` – přijímaný výkon aspoň o 3 dB pod průměrem předchozích 7 dní (≥ 24 hodinových hodnot).
- **Rozhraní:** `site_ports` vrací u portu `health` `{level ok|note|warning|critical, why[], errors_1h,
  errors_24h, ratio, top, kinds, drops, pause, collision, link_downs, sfp_rx, sfp_usual}`; `why` jsou kódy
  (`errors`, `flap`, `sfp`, `half`, `slow`), texty skládá UI. Sekce Porty: značka na dlaždici, filtr
  Zdraví portu, sloupec Chyby 24 h a detail vybraného portu s průběhem po hodinách
  (`GET …/devices/{id}/port-errors?interface=&hours=`).
- Prahy jsou odhad, na skutečném zařízení zatím neověřeno (ani to, které čítače REST vrací).

## Bezdrátové spoje (etapa 16, krok 2)

Modul `wlink.py`, tabulky `wlink` a `wlink_sample` (migrace 0031, RLS, kontrola 20e), krok workeru `links`.

- **Co je spoj:** registrace bezdrátového klienta, jehož MAC je bezdrátové rozhraní jiného spravovaného
  zařízení téže lokality (`find_links`). Strana A = AP/bridge, B = klient. Spoje se hledají z inventur
  každých 10 minut; zmizelý spoj se nemaže (historie, alert výpadku).
- **Měření po 5 minutách (`measure`):** čerstvý řádek registrace z obou stran (signál, vyjednaná rychlost,
  u starého ovladače SNR a CCQ), šum z monitoru rozhraní A (pokud ho ovladač dává) a z A ping 10× po 200 ms
  na adresu B ve společné síti (`ping_ip`) – ztrátovost a odezva. Nedostupná strana B měření neruší
  (typicky je za spojem); nedostupná strana A = žádné měření (řeší alert offline).
- **Normál a hodnocení:** `usual` = medián za 7 dní (aspoň 36 měření). `deviations` hlásí propad signálu
  ≥ 6 dB, rychlost pod polovinou normálu, ztrátovost ≥ 2 %, růst šumu ≥ 6 dB, rozdíl stran ≥ 10 dB, bez normálu
  signál ≤ −78 dBm; k tomu ≥ 3 přepojení za 24 h. `score` 0–100 a stav ok / degraded / bad / down. Rozhoduje
  průměr posledních 3 měření, o výpadku poslední. Váhy a prahy jsou odhad.
- **Alerty:** `link_down` (kritický) a `link_degraded` (varování, při stavu bad kritický) na zařízení strany A;
  spoj jde ztlumit (`muted`, audit `wlink.mute`).
- **API:** `GET …/sites/{id}/links`, `GET|PUT …/links/{id}?hours=` – průběh nejvýš ~290 bodů (delší období se
  průměruje po úsecích, ztrátovost maximem) a `episodes` = souvislé úseky odchylek.
- **Rozhraní:** Zařízení → Spoje. Seznam se stavem, karta spoje: pásy Provoz (z `/traffic` rozhraní A), Signál,
  Rychlost spoje, Ztrátovost a odezva, CCQ, Šum na společné časové ose; úseky odchylek podbarvené přes
  všechny pásy, čárkovaně normál, najetím hodnoty okamžiku, pod grafem seznam úseků.
- **60 GHz (w60g):** inventura čte `/interface/w60g` a monitor každého rozhraní (`wgig_monitor`); spoj se pozná
  podle `remote-address`. Měření bere z monitoru RSSI, `tx-phy-rate` a `tx-packet-error-rate` (uloženo jako
  kvalita = 100 − chybovost ve sloupcích `ccq_*`, v UI „Kvalita přenosu“). Šum ani doba spojení zde nejsou.
- **Mapa:** čára mezi dvěma zařízeními, mezi nimiž je spoj, je tečkovaná v barvě stavu se značkou ≋; klik
  otevře kartu spoje (`openLink`).
  Stejné značení má záložka Topologie (`drawTopology`); spoj, který sousedé neohlásili, se tam doplní jako
  hrana z naměřených spojů. V mapě mají konce bezdrátového spoje značku ≋ místo čísla portu.
- **Propustnost:** tlačítko na kartě pouští existující test `bwtest` (A → B na `ping_ip`, 10 s, TCP, oba směry)
  a ukazuje poslední výsledek.
- **Zatím chybí:** vytížení kanálu, sken spektra, PtMP u 60 GHz (více stanic na jednom AP). Na skutečném
  zařízení neověřeno – zejména názvy polí monitoru w60g.

## Obnova zálohy na jiný server / jinou verzi (doplněk)

- Manifest zálohy nese navíc `pg_major` a `hosts` (`ui`, `checkin`, `sstp`). Starší zálohy bez nich: verze
  PostgreSQL z hlavičky dumpu (`pg_restore -l`), adresy z `config.toml` v záloze.
- `sysbackup.report` posoudí rozbalenou zálohu: `problems` (obnova nepůjde – novější schéma, novější
  PostgreSQL, chybí DB nebo master.key) a `warnings` (adresy nevedou sem, záloha z jiného serveru).
  `restore` tyto kontroly dělá dřív, než sáhne na databázi; platí i pro obnovu z průvodce.
- `mtc-admin restore SOUBOR --check` = jen zpráva, návratový kód 1 při problému. Obnova přes data dělá
  pojistnou zálohu (`reason = pre-restore`), při jejím selhání nezačne (`--no-safety-backup` ji vynechá).
- Downgrade dat (novější záloha do starší aplikace) se záměrně nedělá.

## Vzhled 2, krok 4: záhlaví a panel detailu podle maket 1 a 2

- **Záhlaví:** značka se jménem, pole „Zákazník“ a „Lokalita“ (popisek uvnitř rámečku, uvnitř zůstává nativní
  `select`), vedle zákazníka nabídka **Správa** (`#tmenu`) s položkami Lokality / Uživatelé / Systém – ty už
  nejsou v levém pruhu (ten je jen pro lokalitu). Tlačítka mají dál id `nav-<klíč>` a staví je `buildNav`.
  Vpravo zvonek s počtem k řešení (stejné číslo jako odznak Aktivity, `setBadge`) a nabídka uživatele (`#umenu`:
  změna hesla, jazyk, odhlášení). Přepínač jazyka je před přihlášením v záhlaví, po přihlášení se přesune do nabídky.
- **Detail zařízení:** panel vpravo je užší (600 px) a seznam vedle něj zůstává vidět a klikací; tlačítko ⤢ ho
  rozšíří (`prefs.detailWide`). Hlavička: jméno, pod ním model · role · stav, akce na vlastním řádku. První dvě
  upozornění jsou rovnou barevné pruhy (`#d-banners`), zbytek zůstává ve sbaleném seznamu. Záložky jsou podtržené.
- Zbývá z maket: přejmenování pojmů v celé aplikaci (tenant → zákazník je zatím jen v záhlaví), rozbalovací
  výběr zákazníka s hledáním, přehled „Připojeno“ v detailu podle makety 2.

## Zálohy controlleru: vlastní sekce, stupňovaná retence, Dropbox

- **Sekce:** Správa → Zálohy (`view-backups`, jen provozovatel); karta se přestěhovala ze Systému.
- **Retence (`retention.py`):** `keep` posledních + `weekly` + `monthly`; z období zůstává nejstarší záloha
  (v cloudu se pak nepřepisuje). Čas se bere z názvu souboru, soubory bez času se nemažou. Plán
  (`system_setting.backup`) nese `weekly`/`monthly` pro server, cíle mají sloupce `keep_weekly`/`keep_monthly`
  (migrace 0032). Používá `sysbackup.prune` i `cloudbackup.upload_one`.
- **Dropbox (`cloudbackup.Dropbox`):** OAuth s PKCE bez přesměrování a bez tajemství aplikace –
  `device_start` vrátí odkaz (`paste: true`), správce vloží kód přes akci `code` (`auth_code`). Nahrání
  jedním voláním do 100 MiB, větší relací po 8 MiB (`upload_session/start|append_v2|finish`), výpis
  `list_folder`, mazání `delete_v2`. Ověřeno jen proti napodobenému API.

## Zálohy zařízení (výběr, pravidla zákazníka, verze)

Modul `devbackup.py`, migrace 0033 (`device.backup_enabled`, `backup_error`, `backup_retry_at`, tabulka
`device_backup_policy` s RLS, kontrola 20f). UI: Nastavení → Zálohy zařízení.

- **Výběr:** u každého adoptovaného zařízení (řízeného i jen sledovaného) jde zálohování vypnout.
- **Pravidla (jedna pro zákazníka):** `follow` = termín a retence podle plánu zálohy controlleru,
  `custom` = vlastní interval a retence (posledních N + týdenní + měsíční verze, `retention.select_dated`),
  `only_changes` = nová verze jen při změně konfigurace. Výchozí (bez řádku) odpovídá dřívějšímu chování:
  denně, 365 verzí. `worker.run_backups` bere jen vybraná zařízení starší než hranice zákazníka
  (`devbackup.cutoffs`), po uložení promaže verze (`prune`); chyba se zapíše k zařízení a další pokus je za hodinu.
- **Verze:** `GET …/devices/{id}/backups`; `…/backups/{bid}/diff?against=latest` = co by se změnilo návratem
  k verzi (hesla maskovaná). Stažení exportu zůstává jen pro správce zákazníka a je auditované.
- **Součást zálohy controlleru:** verze jsou šifrovaně v DB, takže jsou v každé záloze controlleru i v cloudu.
- **Obnova na zařízení se zatím nespouští.** Textový export nejde vrátit bez resetu zařízení
  (`reset-configuration run-after-reset`), při kterém zaniknou certifikáty tunelu a REST – vzdálené zařízení by
  se odřízlo. Řešení (reset + export + nová adopce v jednom skriptu, nebo binární zálohy na zařízení) je potřeba
  nejdřív vyzkoušet na skutečném zařízení.

## Poloha klientů za nespravovaným switchem („fantomoví klienti pod AP“)

Problém: AP zapojené přes hloupý switch nebo cizí bezdrátový spoj vidí na svém ethernetu MAC adresy celé sítě
a přes takový úsek neslyší žádného spravovaného souseda (MNDP/LLDP). Dřív se uplink u klientů poznával jen
podle sousedů, takže port AP vyšel jako koncový a drátoví klienti „spadli“ pod AP.

- `clients.port_kinds`: z tabulek MAC adres určí, která spravovaná zařízení jsou za každým portem, a port
  **nahoru** (kudy zařízení vidí bránu; bez označené brány port s nejvíc spravovanými zařízeními).
- `clients.correlate` dělí místa výskytu MAC na `edge` (za portem nic spravovaného), `shared` (za portem je
  i spravované zařízení) a `up`. Klient jde na `edge`; jinak na nejhlubší `shared` port (příznak `shared`);
  port nahoru se použije jen z nouze a s příznakem `uncertain`. Stejná MAC na koncovém portu dvou zařízení =
  `uncertain`. `port_clients` a `unmanaged` říkají, že port sdílí víc klientů / je za ním nespravovaný switch.
- `infra.insert_unmanaged`: kde rodič vede jedním portem ke spravovanému potomkovi a zároveň na něm visí
  drátoví klienti (nebo víc potomků), vloží do mapy uzel `hub` s `guess: true` („nespravovaný switch (odhad)“)
  a potomky i klienty přesune pod něj.
- Nejde zjistit, na kterém portu nespravovaného switche co je, ani kolik jich je za sebou.

## Hlídání nespravovaných switchů a cizích úseků

Navazuje na polohu klientů výše. Tři věci:

- **Bezdrátový spoj mezi spravovanými zařízeními není switch.** `clients.correlate` si z registrací (`wifi_clients`,
  `wireless_clients`) udělá mapu (zařízení, rozhraní) → protistrana. Klient, jehož MAC je vidět jen na takovém
  rozhraní, se zapíše k protistraně s portem `?`, `uncertain` a `via_link` (je někde za vzdálenějším koncem spoje).
  `infra.insert_unmanaged(out, wireless)` na portech z tabulky `wlink` uzel „nespravovaný switch“ nevkládá.
- **Evidence a alert** (`unmanaged.py`, tabulka `unmanaged_port`, migrace 0034). Port je „nespravovaný“, když za ním
  visí aspoň 3 drátoví klienti (2 = telefon s počítačem), nebo klient spolu se spravovaným zařízením. Zapisuje se
  při každé inventuře klientů (`clients.record`). Prvních 24 h od prvního průchodu lokality (`site.unmanaged_scanned_at`)
  se nálezy berou jako výchozí stav (`baseline`) a nehlásí se. Co přibude později a vydrží 10 minut, hlásí alert
  `unmanaged_switch` (warning), dokud to správce neschválí (`approved`, poznámka, audit `site.unmanaged_approve`)
  nebo to nezmizí; neschválený zmizelý nález se po 24 h zapomene. API `GET …/sites/{id}/unmanaged`,
  `PUT …/sites/{id}/unmanaged/{uid}` `{approved, note}`. UI: Zařízení → Spoje, tabulka „Nespravované switche“.
- **Hlídaný úsek** (`wlink.watch` / `unwatch`, `wlink.kind = 'foreign'`, `manual`, migrace 0035): úsek mezi dvěma
  spravovanými zařízeními přes něco nespravovaného zakládá správce ručně – tlačítkem „Hlídat pingem“ u portu
  v Zařízení → Nespravované nebo klikem na odhadnutý nespravovaný switch v mapě (`POST …/sites/{id}/links`
  `{a_device, a_iface, b_device}`, zrušení `DELETE …/links/{id}`, audit `wlink.watch` / `wlink.unwatch`). `measure` dělá
  jen ping z A na adresu B ve společné síti; bez takové adresy úsek založit nejde. Hodnocení, graf a alerty jsou
  společné s bezdrátovými spoji. Automatické hledání (`find_foreign`) se nepoužívá: na skutečné síti dělalo
  z obyčejných switchů desítky planých dvojic a falešné výpadky; migrace 0035 takové záznamy maže.
- **Rozdělení v rozhraní**: levý pruh má samostatnou sekci **Spoje** jen pro rádia (Wi-Fi, wireless, 60 GHz);
  nespravované switche a hlídané úseky jsou v Zařízení → Nespravované.

Omezení: počet klientů za portem kolísá (spící zařízení), proto práh 3 a čekání 10 minut; switch, za kterým jsou
jen 1–2 klienti a žádné spravované zařízení, nepoznáme. Ping přes úsek měří i kabeláž a switche po cestě, ne jen
cizí rádio. Na skutečné síti zatím neověřeno.

## Přehrávání stavu na mapě (posun časové osy)

Osu vedle mapy jde chytit kdekoli a táhnout (nebo točit kolečkem): modrá čára zůstává, pod ní se posouvá čas a místo
„TEĎ“ je vidět zvolený okamžik. Po zastavení posunu (180 ms) se zavolá `GET …/sites/{id}/infra-at?at=ISO`
(`timeline.state_at`): zařízení s tehdy otevřeným alertem `offline`, všechny tehdy otevřené alerty a stav měřených
spojů z nejbližšího vzorku `wlink_sample` (do 20 minut zpět). `itlApply` to promítne do dat mapy (stav uzlů, barvy
spojů), nad plátnem je pruh „Stav k …“ s návratem na teď. Topologii, klienty ani vytížení v čase neukládáme –
přehrává se jen stav nad dnešní mapou. Dosah je rozsah osy (6 h / 24 h / 7 dní).

## Cestování v čase na mapě a přeskakování MAC (`timemachine.py`)

Posun časové osy u mapy vrací celou tehdejší mapu, ne jen stavy. `GET …/sites/{id}/infra-at` (`timeline.state_at`)
k dosavadním polím (offline, alerty, stav spojů) přidává `infra` ve tvaru živé mapy, složené ze tří zdrojů:

- `infra_snapshot` (migrace 0037) – strom zařízení, stav zařízení, porty (linka / rychlost) a linky k internetu.
  Krok workeru `snapshots` každých 5 minut sestaví mapu stejnou funkcí jako API (`infra.site_view`) a uloží ji, jen
  když se liší podpis (bez CPU, RAM, PoE a klientů). Platí poslední snímek před zvoleným časem;
- `client_move` – přesuny klientů (zařízení, port, SSID), zapisuje `clients.record` při změně místa. Poloha klienta
  v čase T = poslední přesun před T;
- `client_session` – kdo byl v čase T připojený.

Když z té doby snímek není (před aktualizací), `infra` je null a rozhraní přehraje jen stavy nad dnešní mapou.
Vytížení (CPU, RAM) a signál klientů se v čase neukládají. Retence 30 dní, poslední snímek a poslední poloha klienta
zůstávají vždy.

**Přeskakování MAC** (`timemachine.alerts`): drátový klient s aspoň 4 přesuny za hodinu, který se vrací na stejná
místa (počet míst < počet přesunů), dává alert `mac_flap`; 5 a víc takových klientů najednou v lokalitě je
`loop_suspect` (critical). Wi-Fi roaming a nejisté polohy (port `?`) se nepočítají. Rozlišení je dané intervalem
inventury – smyčku trvající pár sekund to nezachytí.

## Přehled lokality a měření internetu

Levý pruh má **Přehled** a **Mapu** zvlášť (Mapa = dřívější blok Infrastruktura se záložkami Mapa / Topologie / STP).

Přehled (`dash.site_dash`, `GET …/sites/{id}/dash?hours=`) jen čte, co sbírají jiné moduly:

- vlevo souhrn brány (zařízení s rolí `gateway`): počty zařízení, adresa, doba běhu, verze, linky k internetu
  z `infra._wans` (výchozí routy / DHCP klient), data za měsíc z `interface_hourly`, otevřené alerty lokality;
- graf: provoz WAN rozhraní (stávající `…/devices/{id}/traffic`), odezva a ztrátovost z `wan_probe`, dole pruh
  dostupnosti;
- nejvytíženější AP = součet bezdrátových rozhraní zařízení v `interface_hourly`; nejvytíženější klienti = porty,
  na kterých je drátový klient sám (provoz portu = provoz klienta; u Wi-Fi klientů historii provozu nemáme);
- pod dohledem: bezdrátové spoje a hlídané úseky (`wlink`) a porty s chybami (`porthealth.site_health`);
- Wi-Fi: úspěšnost ověření z `wifi_event` (connected vs. auth_fail) a průměrný signál klientů podle AP.

`wanprobe.py` (tabulka `wan_probe`, migrace 0036, krok workeru `wanprobe`): brána lokality každých 5 minut pingne
1.1.1.1 a 8.8.8.8 (5 paketů přes REST `/ping`). Internet se bere jako dostupný, když odpoví aspoň jeden cíl;
dostupnost = podíl takových měření. Lokalita bez spravované brány se neměří. Retence 30 dní. Rychlost linky
(speed test), toky a aplikace (NetFlow) zatím nejsou.

### Mapa: Topologie a Infrastruktura, porty s časovou osou

- Mapa má vlevo přepínač **Topologie / Infrastruktura** (`mapf.mode`, `localStorage mtc.mapMode`). Infrastruktura =
  jen zařízení, vlevo karty s porty. Topologie = stejný strom i s klienty a vlevo filtry (`renderMapSide`): stav
  zařízení (jen zvýrazní – ostatní zprůhlední), klienti drátoví / Wi-Fi, VLAN, SSID a volba popisků (port u klienta,
  signál a síť u Wi-Fi klientů). Filtruje se v prohlížeči nad daty `…/infra`. Pásmo 2,4 / 5 GHz u klienta
  neukládáme, proto se Wi-Fi nedělí. Dřívější záložka Topologie (tabulka linek ze sousedů) se jmenuje Linky.
- Porty: přepínač Časová osa ukáže vedle schématu události vybraného zařízení (`…/timeline?cats=alert,port,change
  &device=`), klik na událost portu ho vybere ve schématu. Nad schématem jsou upozornění na porty s chybami a na
  porty, za kterými je spravované zařízení offline.

### Nastavení lokality: levé menu, Přehled nastavení a Internet

Nastavení má místo záložek levé menu (`#set-tabs.set-menu`, tlačítka si nechávají id `…-btn`, takže `SUBTABS.settings`
funguje beze změny): Přehled · Wi-Fi · Sítě · Internet · Profily portů · Systém (Zálohy zařízení, Systém a nasazení).

- **Přehled nastavení** (`loadSetOverview`) skládá bloky ze stávajících API: Wi-Fi z `…/wifi` (SSID z rádií v režimu
  AP, počet AP, pásma, klienti), Sítě z `…/networks` (VLAN, podsíť, DHCP server / relay, přidělené a volné adresy),
  Internet z `…/dash` (linky brány, dostupnost a odezva aktivní linky).
- **Internet** (`loadSetInternet`): tabulka linek včetně brány poskytovatele a dat za měsíc a porty brány s tím, co je
  WAN a co vnitřní síť (z `…/ports`). Je to jen přehled – přiřazení portů, přepínání linek podle odezvy a měření
  rychlosti controller nenastavuje.

- **Firewall** (`loadSetFirewall`): zóny (seznamy rozhraní), matice zón a pravidla brány lokality z `firewall.analyze`
  (stejná data jako záložka Firewall v detailu zařízení, bere se `…/devices/{brána}`). Klik na buňku ukáže výjimky
  a rozhodující pravidlo dvojice zón, u pravidel je počet zásahů (packets). Jen čtení.

  Zařazení pravidla do zón (`firewall.Ctx`, `_side`): podle seznamu rozhraní, jednoho rozhraní, adresy (zóna, do jejíž
  podsítě adresa patří; adresa mimo místní podsítě = zóna s linkou k internetu) i seznamu adres (sekce inventury
  `fw_addrlists`, jen nedynamické položky). Shoda je buď úplná (pravidlo pokrývá celou zónu → může rozhodnout), nebo
  částečná (→ výjimka). Výjimky se počítají jen u dvojic, ke kterým se pravidlo váže místem; pravidla s podmínkou
  bez určení místa jsou „obecná“ (`general`) a ukazují se zvlášť. Vypnutá pravidla jsou podbarvená a jdou filtrovat.

Další kroky podle maket 10 a 11 (zatím neuděláno): oznámení po událostech, úpravy firewallu (s plánem a automatickým
vrácením, generátor je v `fwmatrix.py`), VPN server.

## Směr: bezpečnost a brána (rozhodnutí, zatím neimplementováno)

Projekt zůstává nástrojem pro centrální správu MikroTiků. Bezpečnost řešíme tím, co centrální správa umí nejlépe –
hlídáním, že jsou zařízení nastavená bezpečně – a ne vlastním IDS/IPS.

Co neděláme a proč:

- **Zrcadlení provozu z MikroTiku do IDS na controlleru.** Kopie provozu nic nezastaví (jen dodatečný blok adresy),
  celý provoz nejde posílat přes internet a senzor v každé lokalitě je další stroj na údržbu.
- **Řízení OPNsense z controlleru.** Byl by to druhý ovladač brány (firewall, NAT, VPN, DHCP, převzetí dvakrát).
  Vrátíme se k tomu, až o to bude zájem.

Lokalita má **typ brány**:

1. **MikroTik** (dosavadní stav) – controller bránu čte a postupně řídí.
2. **Externí firewall** – před MikroTiky stojí cizí bezpečnostní brána (IPS, HA si řeší sama). Controller ji neřídí.
   Síť za ní spravuje dál; role `gateway` v lokalitě pak nemusí existovat a části, které s ní počítají (Přehled,
   Internet, Firewall, měření internetu), to musí unést.
3. **OPNsense jen pro čtení** – externí firewall, ze kterého přes jeho REST API (klíč + tajemství v `SecretBox`)
   čteme rozhraní, VLAN, adresy, DHCP a stav linek. Nic na něj nezapisujeme.

Sladění sítí s externí bránou (typ 2 ručně, typ 3 automaticky):

- **Controller → brána:** když v controlleru vznikne nebo se změní síť (VLAN ID, podsíť, DHCP), kterou má obsluhovat
  brána, ukáže se úkol „nastav na bráně“ s přesnými hodnotami. U OPNsense se úkol sám uzavře, jakmile ji tam
  controller při čtení najde; rozdíl (jiná podsíť, chybějící VLAN) je upozornění.
- **Brána → controller:** síť, která na OPNsense je a v controlleru ne, se nabídne k převzetí – po potvrzení se
  VLAN roznese na MikroTiky stejnou cestou jako síť založená v controlleru.

Bezpečnost na straně MikroTiku (pořadí práce):

1. **Kontrola zabezpečení** – jen čtení z inventury: služby dostupné z WAN, účty a výchozí hesla, verze RouterOS,
   nešifrované služby, chybějící zálohy; skóre zařízení a lokality s návodem k opravě.
2. Přehled zastaralých verzí (aktualizace už máme), změny konfigurace provedené mimo controller.
3. Odesílání událostí a auditu na syslog (SIEM).
4. Ochrana přímo na bráně MikroTik: seznamy škodlivých adres a zemí jako address-listy, filtrování DNS, NetFlow
   pro přehled provozu. Sahá na bránu → jen s testovacím zařízením.

## Kontrola zabezpečení lokality (`seccheck.py`)

Nastavení → Zabezpečení (`loadSetSecurity`, `GET …/sites/{id}/security`). Jen čtení – nic nového se ze zařízení
neměří, skládají se nálezy z toho, co controller už má:

- audit nastavení zařízení (`hardening.py`, sekce inventury `hardening`) – nově i UPnP, SMB a nastavení SSH
  (sekce `upnp`, `smb`, `ssh`; zařízení bez těchto menu sekci prostě nemá),
- verze RouterOS (`ros_versions.py`: nepodporovaná, známá chyba; sekce `update`: zastaralá proti kanálu –
  o 4 a víc vedlejších verzí je varování, jinak informace),
- zálohy zařízení (vypnuté, selhaly, žádná, starší než 8 dní), synchronizace času,
- účty zákazníka bez dvoufázového ověření (nález lokality, oblast `controller`).

Každý nález má oblast (`exposure`, `access`, `updates`, `backup`, `time`, `controller`). Skóre zařízení = 100 −
srážky (kritický 30, varování 10, informace 2), s kritickým nálezem nejvýš 40. Skóre lokality = průměr zařízení −
srážky za nálezy lokality, s kritickým nálezem nejvýš 49. Zařízení bez inventury nastavení (nespravované přes API)
skóre nemá a do průměru se nepočítá. Čísla jsou orientační, ne norma.

Oprava jedním klikem zůstává jen u nálezů z `hardening.FIXES` (plán s automatickým návratem); ostatní mají příkaz
ke zkopírování.

## Provoz podle klientů – NetFlow z brány (`flow.py`)

Aktivita → Provoz (`loadFlow`, `GET …/sites/{id}/traffic?hours=&client=`). Brána lokality posílá NetFlow v9 tunelem,
controller z toků skládá hodinové součty. Obsah provozu se nevidí – jen kdo s kým, protokol, port a objem.

- **Cesta dat** je stejná jako u logů: brána posílá na tunelovou adresu koncentrátoru, ten toky přeloží (dst-nat)
  na controller a zdroj zůstane tunelová IP brány; podle ní se tok přiřadí zařízení. Pravidla koncentrátoru
  (`mtc: netflow ze zarizeni`, `mtc: netflow na controller`) jsou ve skriptu z `concentrator.py` – **na už běžící
  koncentrátor je potřeba skript nahrát znovu** a na controlleru povolit UDP `flow.port` (2055) od koncentrátoru.
- **Příjem** běží ve službě `mtc-syslog` (druhý UDP port vedle syslogu; když ho nejde otevřít, logy běží dál).
  `flow.Parser` čte NetFlow v5, v9 a IPFIX podle šablon (šablony po zdroji a source id), neznámá pole přeskočí.
- **Zapnutí** po bráně (`device.flow_enabled`, `POST …/devices/{id}/flow`, příkaz `flow` přes check-in, audit
  `device.flow`): na bráně jen cíl v `/ip traffic-flow target` (pozná se podle adresy a portu) a `enabled=yes
  active-flow-timeout=1m`. Cizí cíle zůstávají; při vypnutí se traffic-flow vypne, jen když jiný cíl není.
- **Klient a směr** (`classify`): klient = strana s neveřejnou adresou. U příchozího toku je cílem veřejná adresa
  brány a klient se bere z adresy po překladu (pole 226). IPv6 klienti mají veřejné adresy – tam je klientem zdroj.
- **Uložení** `flow_hourly` (migrace 0038): brána × hodina × klient × protistrana × protokol × port služby →
  bajty ven / dovnitř, pakety, toky. Zápis po 5 minutách; na bránu a hodinu nejvýš `flow.max_rows_per_hour` (500)
  řádků, zbytek se sečte po klientech do řádku „ostatní“ (peer 0.0.0.0, proto 0, port 0); uzavřené hodiny se
  jednou za hodinu doúklízí (`compact`). Retence `flow.retention_days` (30). Pojistka `flow.max_packets_per_minute`.
- **Omezení:** brána vidí jen provoz přes své CPU (ne provoz přepínaný čipem uvnitř VLAN). Jména protistran se
  nepřekládají, služba je odhad z portu. Na skutečném RouterOS zatím neověřeno – hlavně jaká pole šablona nese
  (adresa po NATu) a zda FastTrack toky nezkresluje.

**Oprava po nasazení na skutečné síti (migrace 0039):** přeskakování MAC a podezření na smyčku počítají jen
jisté polohy (`client_move.firm` – klient na koncovém portu). Poloha odhadnutá na sdíleném nebo páteřním portu
(uplink, bonding, VLAN rozhraní, spoj) se mezi inventurami mění sama a dávala plané kritické poplachy.
Hlídané úseky, které jsou dole ke stejné protistraně, dávají jeden alert u protistrany místo jednoho za úsek.
Přehled: rádio, které drží spoj mezi našimi zařízeními, se nepočítá mezi nejvytíženější AP.
Hlídaný úsek si při každém objevování spojů (`wlink.discover`) přepočítá adresu protistrany z aktuální inventury –
po změně adresy z DHCP se pinguje na novou. Karta klienta má záložku **Provoz** (`loadClientTraffic`): graf portu,
na kterém klient je (`…/devices/{id}/traffic`), a pokud brána sbírá NetFlow, i služby a protistrany podle jeho IP.
Otevírá se i klikem na klienta v Přehledu.

## Bezpečnostní revize (listopad 2026)

Prošlo se přihlášení a relace, oprávnění koncových bodů, izolace zákazníků (RLS), skládání SQL, skripty RouterOS,
ohlašování zařízení, tajemství, nahrávání souborů, vkládání do stránky a spouštění procesů. Opraveno:

- **Hádání kódu MFA.** Ověření kódu nemělo limit a správné heslo nulovalo počítadlo chybných pokusů – kdo znal
  heslo, mohl šestimístný kód zkoušet donekonečna. Chybný kód se teď počítá jako chybné heslo, počítadlo nuluje až
  dokončené přihlášení, po pěti chybách se účet na 15 minut zamkne a rozdělané relace (bez MFA) skončí. Zamčený
  účet nepustí ani správný kód. K tomu limit 10 pokusů za minutu na uživatele.
- **Tokeny v přístupovém logu.** Adopční token a klíč lokality jsou v cestě URL (RouterOS fetch to jinak neumí).
  Uvicorn je do logu píše jako `***` (`web.app.RedactTokens`), šablony nginx takové požadavky nelogují
  (`map $request_uri $mtc_loggable`). U už nainstalovaného nginx je potřeba šablonu přenést ručně.
- **Paměť příjmu NetFlow.** Strop klíčů v paměti platí i pro řádky „ostatní“ – adresy klientů si odesílatel může
  vymýšlet.
- **Export zápůjček do CSV.** Buňka začínající `= + - @` (hostname posílá klient v síti) se uvozuje apostrofem,
  aby ji tabulkový procesor nespustil jako vzorec.
- Drobnosti: úklid slovníku limitu přihlášení, jméno rozhraní v doporučeném příkazu plánu kanálů přes `q()`,
  karta klienta z Přehledu nebere seznam klientů jiné lokality.

Vědomě ponecháno (nízké riziko, zapsáno, ať se na to nezapomene):

- Přidání uživatele prozradí správci zákazníka, zda e-mail už v controlleru má účet (jiného zákazníka).
- `role_binding` a `app_user` nejsou pod RLS (potřebuje je přihlášení); dotazy je filtrují podle zákazníka v kódu.
- Sériové číslo si zařízení při ohlášení hlásí samo – kdo má klíč lokality s automatickou adopcí, zařízení přidá.
  Klíč lokality je proto tajemství; jde otočit.
- Syslog a NetFlow jsou UDP bez ověření – v lokálním režimu je může kdokoli v síti podvrhnout pod adresou brány.
- Zámek účtu po pěti chybách jde zneužít k dočasnému zablokování cizího účtu (kdo zná e-mail a dostane se na
  přihlašovací stránku, která je jen ze sítě správy).

**Wi-Fi klienti s CAPsMANem.** Tabulka připojených klientů je na správci (CAPsMAN), klient ale patří AP, jehož
rádio ho drží. `clients.correlate` proto rozhraní správce překládá přes `wifidiag.iface_map`: podle MAC rozhraní,
podle MAC rádia a jména CAP (`/interface/wifi/radio` na správci, pole `cap` = identita AP, jen když ji má právě
jedno zařízení) a přes `master-interface` pro další SSID. Klient pak má `device` = AP a `port` = rádio AP;
od toho se odvíjí mapa, Přehled (signál podle AP, počty klientů) i historie polohy. Starý CAPsMAN (`/caps-man`)
se nepřekládá.

## Klienti → Rozbor připojení (`clientview.py`)

Výchozí pohled sekce Klienti (`loadClientView`, `cv*` v app.js; původní tabulka je druhá záložka „Seznam“).
Rozložení podle UniFi: filtry (stav, drát / Wi-Fi, AP, Wi-Fi síť, VLAN, výrobce) → seznam klientů → svislá časová
osa událostí → cesta klienta k internetu a tři grafy na společné časové ose (klient, jeho AP, lokalita) →
záznam připojení / provoz z NetFlow.

Jedna odpověď `GET …/sites/{id}/client-insight?mac=&hours=` skládá, co už máme: `wifidiag.client_journey`
(dráhy AP, signál, přechody, odpojení, nálezy), `infra.site_view` (cesta přes nadřazená zařízení k bráně),
skutečný kanál rádia (`wifi_monitor` na AP nebo rozhraní CAP na správci), počet klientů AP v čase (ze
`wifi_sample`, rozhraní správce přeložená na AP), cizí sítě na kanálu (`wifi_bss`) a `wanprobe.series`.
Provoz rádia AP, provoz portu u klienta na kabelu a NetFlow klienta si rozhraní dotahuje ze stávajících
koncových bodů.

Nově se ukládá jen přenesený objem Wi-Fi klienta: `wifi_sample.tx_bytes/rx_bytes` (migrace 0040) z pole `bytes`
registrační tabulky; `clientview.activity` z čítačů počítá rozdíly (čítač, který klesl, nebo jiné rádio = nové
připojení).

Co proti UniFi chybí, protože to RouterOS přes API nedává: opakované přenosy (TX retries), vytížení éteru,
rušení, seznam AP, která klienta slyší (ukazují se AP, na kterých klient v období skutečně byl), a rozpoznání
aplikací.

**Přihlášení a ověření ve dvou krocích** mají vzhled průvodce instalací (třídy `.auth*` ve style.css: přechodové
pozadí, karta, značka). Nastavení 2FA při prvním přihlášení ukazuje QR, tři kroky a klíč ke zkopírování; kód se
po šesté číslici odešle sám. Chyby se píšou přímo do formuláře (`authErr`). Když se QR nepodaří připravit,
je důvod vidět (dřív se chyba tiše zahodila a QR prostě chyběl); SVG s QR má pevné rozměry.

## Příprava zařízení před nasazením (`prep.py`)

Zařízení → Příprava: průvodce, kterým se naklikne konfigurace kusu, který ještě není v síti
(dnes jen role switch).

- **Podklad**: `prep.context()` vrací sítě lokality (z nastavení lokality, u lokality mimo řízení
  z VLAN zjištěných na zařízeních), VLAN správy a seznam modelů – vestavěné, modely zařízení, která
  controller zná (jména portů z inventáře), nebo ruční zadání počtu portů (`ether1…N`, `sfp-sfpplus1…M`).
- **Návrh** (`device_prep`, migrace 0041, RLS podle tenanta): jméno, model, volitelné sériové číslo,
  porty (`access` / `hybrid` / `trunk` / `off`, STP edge, PoE, popis), správa (VLAN, nativní na trunku,
  DHCP nebo pevná adresa + brána + DNS), co s nenastavenými porty (vypnout / výchozí VLAN).
  Stav `draft` (má blokující chybu) → `ready` → `deployed`.
- **Skript**: `prep.build()` převede návrh na model pro `switchcfg.config_body()` – stejný generátor
  jako převzetí switche do řízení, žádná druhá cesta. K němu enroll smyčka z `defaults`. Blokující chyby
  (VLAN správy nevede ven žádným portem, pevná adresa mimo síť…) skript nevydají.
- **Doručení**: jednořádkový příkaz do terminálu (stáhne `/enroll/{klíč}/prep/{id}` a resetuje zařízení
  na skript), nebo soubor `.rsc` pro reset s `run-after-reset` / Netinstall.
- **Párování**: `services.register_device()` volá `prep.pair()` – podle sériového čísla, bez něj jediný
  připravený návrh téhož modelu na lokalitě. Návrh přejde do
  `deployed`; schválení zůstává ruční. Párování je v auditu `device.register`.
- **Rozhraní**: schéma portů s výběrem tažením, Ctrl+klik a Shift+klik (rozsah), klávesnicí Enter/mezerník;
  průběžná kontrola, že VLAN správy vede ven.
- **Doručení bez spojení s controllerem** je první volba v kroku kontroly: soubor `.rsc` + příkaz pro reset
  s `run-after-reset` (sám pozná složku `flash/`). Jednořádkový příkaz je jen pro kus, který na controller vidí.
- **Spanning tree**: návrh nese režim (RSTP / MSTP) a u MSTP jméno a revizi regionu; výchozí hodnota je to,
  co převažuje na bridgích lokality (inventura čte `region-name`, `region-revision`). VLAN zůstávají v CIST,
  mapování do MSTI se nepřenáší (průvodce na to upozorní).
- **VLAN správy u lokality mimo řízení** (`_guess_mgmt`): síť, ve které má adresu nejvíc zařízení; netagovaná
  síť se stejným rozsahem (táž síť viděná z brány) se z nabídky vypustí.
