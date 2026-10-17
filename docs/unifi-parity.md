# UniFi → MikroTik Controller: mapování funkcí

Stav k 2026-10-17. Cíl: nabídnout to, co dělá UniFi Network / Site Manager
užitečným, nad zařízeními MikroTik, a přitom nepřepisovat konfiguraci, kterou
správce nepředal do správy controlleru.

## Princip převzetí (rozdíl proti UniFi)

UniFi zařízení po adopci vždy plně řídí controller. MikroTik v terénu má
existující konfiguraci, kterou nelze bezhlavě přepsat. Proto:

- Adopce mění jen objekty `mtc` (tunel, servisní účet, check-in).
- Každý modul má na zařízení režim **observe** (výchozí – jen čte a hlásí
  odchylky) nebo **manage** (zapíná admin, předem vidí plán změn).
- Změny jdou vždy jako **plán** (vygenerovaný skript + popis), který admin
  schválí; aplikace je auditovaná.
- Konfigurace, kterou controller nespravuje, zůstává netknutá.

To odpovídá UniFi **Blueprints + Drift Inspector**: požadovaný stav lokality,
viditelná odchylka zařízení, vědomé srovnání.

## Mapování

| UniFi | MikroTik / RouterOS v7 | Modul controlleru | Stav |
| --- | --- | --- | --- |
| Adopce zařízení | `/tool fetch` + import, SSTP tunel | adopce | hotovo (F0) |
| Multi-site, role, MSP skupiny | – | tenanti, lokality, role | hotovo (F0) |
| Admin Activity log | – | audit_log (append-only) | hotovo (F0) |
| Remote management (Site Manager) | outbound 443 (SSTP + HTTPS) | konektivita | hotovo (F0) |
| Site overview, seznam zařízení | `/system/resource`, `/system/routerboard` | inventura | F1 – tato noc |
| Backups | `/export` + `/file/read` po 32 KiB přes REST | zálohy | F1 – tato noc (experimentální) |
| Update Manager | `/system/package/update`, `/system/routerboard` | verze a aktualizace | aktualizace RouterOS a RouterBOOT jedním tlačítkem (blokuje verze s kritickým známým problémem, alert když se nepovede); postupná aktualizace lokality hotová |
| Release notes u aktualizace | `download.mikrotik.com/routeros/<verze>/CHANGELOG` | aktualizace | changelogy všech mezilehlých verzí, filtr na komponenty, které zařízení používá; důležité změny (`!)`) nahoře |
| Topology | `/ip/neighbor` (MNDP/LLDP), bridge host | topologie | F2 (sousedé už v inventuře) |
| Client list / insights | DHCP lease, ARP, bridge host, wifi registration | klienti | hotovo (seznam + poloha) |
| Port Manager, VLAN viewer | `/interface/bridge/port`, `/interface/bridge/vlan`, `/interface/ethernet/monitor`, `poe/monitor` | VLAN | tabulka portů (rychlost, duplex, SFP, PoE, PVID/tagované VLAN) + analýza hotové (jen čtení), správa F3 |
| STP stav (port insights) | `/interface/bridge/monitor`, role portů v `/interface/bridge/port` | STP | root bridge, blokované porty, varování (STP vypnuté s více linkami, cizí root) – jen čtení |
| Networks (VLAN per lokalita) | bridge vlan-filtering, VLAN rozhraní | VLAN | F3 |
| Blueprints + Drift Inspector | – | politiky lokality + plány | základ tato noc (NTP) |
| NTP / čas (UniFi řeší implicitně) | `/system/ntp/client`, `/system/ntp/server` | NTP | tato noc (první zápisový modul) |
| WiFi networks (SSID) | `/interface/wifi`, `/interface/wireless` + CAPsMAN | CAPsMAN | přehled rádií/SSID/klientů/signálu a CAPsMAN AP hotový (jen čtení); správa SSID F4 |
| Radio Manager, RF scan | `/interface/wifi/radio`, `/interface/wifi/scan` | CAPsMAN | F4 |
| Alerty a notifikace | netwatch, log, polling | alerty | F1 (offline už je) |
| ISP Viewer (latence, ztráty, uptime) | `/tool/netwatch`, ping z gatewaye | WAN monitor | F1 |
| Traffic Insights | traffic-flow (IPFIX); DPI v RouterOS není | provoz | F2 |
| Speed test | `/tool/speed-test`, `/tool/bandwidth-test` | diagnostika | F2 |
| Site Magic SD-WAN | WireGuard mezi gatewayi | site-to-site | později |
| Guest portal, vouchery | Hotspot | hotspot | později |
| Zone-based firewall | `/ip/firewall` | firewall | F5 (address-listy → vlastní chainy → plná správa) |
| Remote web/SSH na zařízení | – | PAM přes controller | F3 |

## Hlídání vývoje RouterOS

RouterOS se mění rychle a opravy i regrese přicházejí v patch verzích.
Controller proto má tabulku známých problémů (`ros_versions.py`), které
se ho týkají, a ukazuje je u zařízení. Aktuálně v ní je:

| Verze | Problém | Dopad na controller | Zdroj |
| --- | --- | --- | --- |
| 7.24 – 7.24.4 | Scheduler s výchozím startem se nespouští (opraveno v 7.24.5) | Check-in by neběžel – obejito explicitním startem | [7.24.5 changelog](https://forum.mikrotik.com/t/7-24-5-stable-is-released/273494) |
| 7.24+ | Hlášený únik paměti u skriptů ze scheduleru s velkým výstupem (SUP-222576) | Check-in výstup potlačuje (`as-value`); sledovat paměť | [vlákno 7.24](https://forum.mikrotik.com/t/7-24-stable-is-released/272381?page=8) |

Další poznatky z průzkumu:

- RouterOS v7 má nově i kanál long-term (LTS) – controller ho zobrazuje
  a do budoucna umožní vybrat kanál per tenant/lokalita.
- REST API má limit 60 s na požadavek a neumí průběžný monitoring
  (`monitor` jen s `once`) – polling proto pracuje s krátkými dotazy
  ([REST API](https://help.mikrotik.com/docs/spaces/ROS/pages/47579162/REST%2BAPI)).
- Obsah souboru přes API dříve šel jen do 4 KiB; `/file/read` čte po
  částech až 32 KiB, čímž jde stáhnout celý export
  ([fórum](https://forum.mikrotik.com/t/max-variable-size-and-reading-from-or-writing-to-files/183209)).

Do budoucna: worker bude jednou denně porovnávat nainstalované verze
s dostupnými (`/system/package/update/check-for-updates` na zařízení)
a admin dostane přehled „co je zastaralé a proč".

## Zdroje k UniFi

- [UniFi Remote Management via Site Manager](https://help.ui.com/hc/en-us/articles/20680072882967-UniFi-Remote-Management-via-Site-Manager)
- [UniFi site management (Blueprints, Drift Inspector, Update Manager, ISP Viewer)](https://www.unihosted.com/blog/unifi-site-management)
- [UniFi Network Application 8 – Radio Manager, Port Manager, VLAN viewer, Admin Activity](https://virtualizationhowto.com/2023/12/unifi-network-application-8-new-features-and-enhancements)

## Navíc oproti UniFi

- Bezpečnostní audit konfigurace každého zařízení (`hardening.py`): veřejná IP bez filtru na input, otevřený DNS resolver, nešifrované služby, MAC server na WAN, SNMP public, SOCKS/proxy (častý znak kompromitace), výchozí `admin`. Každý nález s návrhem opravy.
- Release notes filtrované na komponenty, které konkrétní zařízení používá.
- STP přehled s detekcí rizika smyček.

## UniFi Network 9.x–10.x (2025–2026) – novinky a jejich protějšek v RouterOS

Rešerše 17. 10. 2026 (zdroje dole). Stav: ✅ hotovo · 🟡 částečně · ⏳ plán.

### Switche

| UniFi | Co dělá | RouterOS / náš návrh | Stav |
| --- | --- | --- | --- |
| Infrastructure Topology (10.2) | mapa páteře: WAN → gateway → switche, STP, CPU, rozpojené linky | strom ze sousedů MNDP/LLDP, WAN z výchozích rout, STP root, CPU/RAM, porty na spojích (`infra.py`) | ✅ |
| Port Manager + Ports Insights | přední panel, profily portů, PoE, VLAN | panel portů s pohledy + editor portu (zapnutí, popis, PoE, PVID, tagované VLAN, STP edge, restart PoE) | ✅ |
| Time Machine portů (10.2, 10.6) | 24h historie stavů portů | `port_event` z porovnání inventur (linka, výpadky z `link-downs`, rychlost, duplex, PoE, PVID, tagované VLAN, STP role, soused, popis), 90 dní; časová osa v detailu zařízení a v editoru portu | ✅ (přetáčení panelu portů v čase zbývá) |
| Port Anomaly (9.5) | flapping, smyčky, včasné alerty | alerty flapping, chyby, half-duplex ✅; ⏳ `loop-protect` na ethernet portech + alert | 🟡 |
| STP Edge / Auto STP Edge (10.2) | edge porty k zařízením, rychlejší konvergence | bridge port `edge=yes/auto`; varování „konvergence STP“ v Infrastruktuře | 🟡 |
| Ethernet Port Profiles | šablona portu (VLAN, PoE, STP, storm control) | profil = sada vlastností bridge port + bridge vlan + ethernet (poe-out, storm-rate…); aplikace přes plán změn s potvrzením | ◐ profily tenanta (PVID, tagované VLAN, PoE, edge), výběr v editoru portu, shoda portu s profilem; hromadné použití a storm control zbývají |
| Port locking (10.6) | port uzamčený na konkrétní zařízení | bridge port `learn=no` + statický bridge host, nebo `/interface bridge filter` s MAC | ⏳ |
| Device Supervisor (10.2) | ping watchdog, power-cycle přes PoE | controller pingne zařízení přes REST souseda → `/interface ethernet poe power-cycle` na portu switche (se schválením) | ⏳ |

### Wi-Fi / AP

| UniFi | Co dělá | RouterOS / náš návrh | Stav |
| --- | --- | --- | --- |
| Radio Manager, Radios Overview + Time Machine | rádia, kanály, klienti, historie | přehled Wi-Fi (rádia, SSID, kanály, signál, CAPsMAN) | 🟡 bez historie |
| Channel AI (9.5) | noční plán kanálů pro celou lokalitu | `/interface wifi scan` / frequency-scan na AP → návrh plánu kanálů → plán změn | ⏳ |
| Multicast Suppressor | méně broadcastů ve vzduchu | `multicast-enhance`, IGMP snooping na bridge, filtrace mDNS | ⏳ |
| OWE (Enhanced Open) | šifrované otevřené SSID | `security.authentication-types=owe` (wifi balík) | ⏳ |

### Gateway, firewall, routování

| UniFi | Co dělá | RouterOS / náš návrh | Stav |
| --- | --- | --- | --- |
| Zone-Based Firewall (9.0) | zóny (Internal, External, Gateway, VPN, Hotspot, DMZ), matice zón, politiky Allow/Block, „auto allow return traffic“ | zóna = `/interface list`; politika = pravidlo ve `forward` (mezi zónami) nebo `input` (zóna → Gateway) s `in-/out-interface-list`; návratový provoz = `established,related` na začátku; objekty = `address-list`; plán = `time=` | ◐ matice zón, pravidla filtru a NAT jen pro čtení (`firewall.py`); úpravy s commit-confirm zbývají |
| Master Policy Table (9.4) | firewall, NAT, routy, QoS na jedné stránce | jedna tabulka z `/ip firewall filter`, `nat`, `mangle`, `/ip route`, `/queue` | ◐ filtr + NAT v detailu zařízení (jen čtení) |
| Object Oriented Networking (9.4) | pravidla pro skupiny klientů | address-listy plněné z DHCP (`address-lists=` na lease / síti) | ⏳ |
| High Availability Readiness (10.6) | skóre: ISP, napájení, gateway jako SPOF | víc WAN (výchozí routy s `distance`/`check-gateway`) ✅ v Infrastruktuře; ⏳ skóre (2 WAN, 2 koncentrátory, VRRP, záložní napájení PSU z `/system health`) | 🟡 |

### Správa

| UniFi | Co dělá | RouterOS / náš návrh | Stav |
| --- | --- | --- | --- |
| SafeOps (10.6) | test-and-confirm, návrat na poslední funkční konfiguraci | commit-confirm: návratový skript + scheduler s počítadlem, potvrzení dalším check-inem | ✅ pro porty (ověřit na HW) |
| Granular bulk updates (10.6) | řízené hromadné aktualizace | postupná aktualizace lokality | ✅ |
| Firmware revert (10.2) | návrat verze | `/system package downgrade` (balíčky nahrát) – s varováním | ⏳ |
| Drift Inspector (Site Manager) | odchylky konfigurace mezi lokalitami | drift ze záloh ✅; ⏳ porovnání se šablonou lokality | 🟡 |
| Topology Spotlight (10.6) | zvýraznění důležitých klientů | ⏳ v Infrastruktuře zvýraznit cestu k vybranému klientovi | ⏳ |
| Alarm Manager | vlastní pravidla upozornění | alerty + notifikace (webhook, Talk, e-mail) ✅; ⏳ uživatelské prahy | 🟡 |

### Doporučené pořadí

1. **Commit-confirm (SafeOps)** – bez něj nejde bezpečně měnit VLAN ani firewall. Ověřit test-plan 9b body 5–6.
2. **Profily portů + změny portů** (PVID, tagované VLAN, PoE on/off, power-cycle, edge) – nejčastější každodenní práce na switchích.
3. **Firewall – matice zón jen pro čtení** ze stávajících pravidel (interface listy jako zóny), pak politiky s návratem.
4. **Časová osa portů** (Time Machine) – uložit změny stavu portů při každé inventuře.
5. **Channel plán pro Wi-Fi** a OWE.

### Zdroje

- [Introducing UniFi Network 10.6](https://blog.ui.com/article/introducing-unifi-network-10-6)
- [What's new with UniFi Network 10.2 (HostiFi)](https://www.hostifi.com/blog/what-is-new-with-unifi-network-10-2)
- [UniFi Network 10.2 now available (Dong Knows Tech)](https://dongknows.com/unifi-network-10-2-now-available/)
- [Releasing UniFi Network 9.5](https://blog.ui.com/article/releasing-unifi-network-9-5)
- [What's new in UniFi Network 9.4.17 (HostiFi)](https://www.hostifi.com/blog/what-is-new-in-unifi-network-9-4-17)
- [UniFi Zone-Based Firewall (LazyAdmin)](https://lazyadmin.nl/home-network/unifi-zone-based-firewall/)
