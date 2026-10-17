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
| Update Manager | `/system/package/update` | verze a aktualizace | F1 přehled; hromadný rollout F3 |
| Topology | `/ip/neighbor` (MNDP/LLDP), bridge host | topologie | F2 (sousedé už v inventuře) |
| Client list / insights | DHCP lease, ARP, bridge host, wifi registration | klienti | F2 |
| Port Manager, VLAN viewer | `/interface/bridge/port`, `/interface/bridge/vlan` | VLAN | analýza (observe) tato noc, správa F3 |
| Networks (VLAN per lokalita) | bridge vlan-filtering, VLAN rozhraní | VLAN | F3 |
| Blueprints + Drift Inspector | – | politiky lokality + plány | základ tato noc (NTP) |
| NTP / čas (UniFi řeší implicitně) | `/system/ntp/client`, `/system/ntp/server` | NTP | tato noc (první zápisový modul) |
| WiFi networks (SSID) | `/interface/wifi` + CAPsMAN | CAPsMAN | F4 |
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
