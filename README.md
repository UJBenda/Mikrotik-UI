# MikroTik Controller

A self-hosted, multi-tenant controller for MikroTik RouterOS v7 in the spirit of
UniFi Network: central management of switches and access points, site topology,
client tracking, VLANs, Wi-Fi through CAPsMAN, backups and updates.

Devices connect to the controller with outbound connections only (HTTPS check-in,
optionally an SSTP tunnel on port 443), so they work behind NAT and CGNAT and need
no public address.

> Not affiliated with MikroTik or Ubiquiti. The project is young: it is developed and
> tested mostly against a lab, so try it on non-critical devices first.

Česká verze: [docs/README.cs.md](docs/README.cs.md).

## What it does

- **Adoption in one step** – paste one command into the MikroTik terminal, pick a role
  (switch, AP + switch, monitor only); optional automatic adoption of factory-default devices.
- **Managed or monitored** – managed devices are configured from the site intent
  (networks, port exceptions, LACP bonds, Wi-Fi); monitored devices are only read.
- **Save = deploy, safely** – every change is applied with commit-confirm: if the device
  does not report back within 5 minutes, it reverts by itself. Changes that would cut
  off management are refused up front.
- **Switching** – site networks (VLANs), port profiles, port editor, LACP (also on
  non-adjacent ports), STP view, loop protect, PoE control and watchdog, port history.
- **Wi-Fi** – one CAPsMAN per site, SSIDs with groups and per-password VLANs, roaming,
  channel and power optimisation from measured surroundings, rogue AP scan.
- **Clients** – where each client is connected, search across sites, connection history
  with a time slider, DHCP reservations and options, vendor by MAC.
- **Operations** – RouterOS and RouterBOOT updates one device at a time from the edge of
  the network (canary, scheduling, automatic updates in a maintenance window),
  encrypted configuration backups with diff, alerts with maintenance windows,
  notifications (e-mail, webhook, Nextcloud Talk).
- **Multi-tenant** – tenants are isolated in PostgreSQL with row-level security;
  roles per tenant, mandatory TOTP MFA, append-only audit log.
- **Languages** – the interface ships in Czech and English; other languages can be added
  as a single JSON file, see [docs/TRANSLATING.md](docs/TRANSLATING.md).

## Requirements

- Debian 13 (native, systemd; no Docker), PostgreSQL, nginx – everything from Debian packages
- MikroTik devices with RouterOS v7 (REST API)

## Install

The latest build is in [`packages/`](packages/).

```sh
sudo apt update && sudo apt install git
git clone https://github.com/UJBenda/Mikrotik-UI.git
sudo cp Mikrotik-UI/packages/mtcontroller_*_all.deb /tmp/
sudo apt install /tmp/mtcontroller_*_all.deb
```

Use `apt install`, not `dpkg -i`, so dependencies are pulled in. At the end the installer
prints the address of the setup wizard and a one-time code; the wizard asks for the mode
(local network or remote management), administrator account and first site, generates
certificates and the backup password, and shows a recovery sheet to store safely.

Afterwards check the installation with `sudo mtc-admin doctor`.

The setup wizard, server-side messages (errors, alerts, notifications) and the design
documents are currently in Czech only; the administration interface is fully translated.

## Development

```sh
apt install python3-starlette python3-uvicorn python3-psycopg python3-pydantic \
            python3-cryptography python3-pytest postgresql ieee-data nodejs
python3 -m pytest                  # unit and end-to-end tests (skipped without a database)
sudo -u postgres tests/db/run.sh   # schema checks (row-level security, audit, tokens)
node tests/ui/uitest.js src/mtcontroller/web/static/app.js \
     src/mtcontroller/web/static/index.html tests/ui/api-fixture.json
python3 tools/i18n.py check en     # every interface text has a translation
```

The stack is deliberately small and comes from Debian only: Starlette, uvicorn, psycopg,
pydantic, cryptography, vanilla JavaScript without a build step. Architecture notes
(Czech): [docs/architecture.md](docs/architecture.md).

## License

A license has not been chosen yet. Until one is added to the repository, the usual
default applies: you may read the code, but no rights to use, modify or redistribute
it are granted.
