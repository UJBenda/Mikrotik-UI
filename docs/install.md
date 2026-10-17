# Instalace na Debian 13

Předpoklad: čistý Debian, veřejný hostname pro check-in, admin hostname
dostupný jen z admin sítě / VPN.

## 1. Balíčky a uživatel

```sh
apt install python3 python3-venv postgresql nginx certbot python3-certbot-nginx nftables
adduser --system --group --home /var/lib/mtcontroller mtcontroller
install -d -o root -g mtcontroller -m 0750 /etc/mtcontroller
install -d -o mtcontroller -g mtcontroller -m 0700 /var/lib/mtcontroller
```

## 2. Aplikace

```sh
git clone https://github.com/UJBenda/Mikrotik-UI /opt/mtcontroller/src
python3 -m venv /opt/mtcontroller/venv
/opt/mtcontroller/venv/bin/pip install /opt/mtcontroller/src
```

## 3. Databáze

Dvě role: `mtc_owner` vlastní schéma (jen migrace), `mtc_app` je
aplikace – **bez BYPASSRLS**, takže row-level security platí vždy.
Obě se přihlašují přes unix socket (peer auth), bez hesla po síti.

```sh
sudo -u postgres psql <<'EOF'
CREATE ROLE mtc_owner LOGIN;
CREATE ROLE mtc_app LOGIN NOBYPASSRLS;
CREATE DATABASE mtcontroller OWNER mtc_owner;
\c mtcontroller
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO mtc_owner;
EOF
```

`/etc/postgresql/16/main/pg_ident.conf`:

```
mtc   mtcontroller   mtc_app
mtc   mtcontroller   mtc_owner
```

`/etc/postgresql/16/main/pg_hba.conf` (před ostatní řádky):

```
local  mtcontroller  mtc_app,mtc_owner  peer map=mtc
```

`listen_addresses = 'localhost'` v `postgresql.conf` (výchozí).

## 4. Konfigurace a master klíč

```sh
cp /opt/mtcontroller/src/config.toml.example /etc/mtcontroller/config.toml
/opt/mtcontroller/venv/bin/mtc-admin gen-master-key > /etc/mtcontroller/master.key
chown root:mtcontroller /etc/mtcontroller/config.toml /etc/mtcontroller/master.key
chmod 0640 /etc/mtcontroller/config.toml /etc/mtcontroller/master.key
```

**Master klíč zálohuj offline** (šifrovaný USB / trezor). Bez něj nejdou
dešifrovat hesla zařízení ani zálohy konfigurací.

## 5. Migrace a první admin

```sh
cd /opt/mtcontroller/src
sudo -u mtcontroller MTC_CONFIG=/etc/mtcontroller/config.toml \
  /opt/mtcontroller/venv/bin/alembic upgrade head
sudo -u mtcontroller MTC_CONFIG=/etc/mtcontroller/config.toml \
  /opt/mtcontroller/venv/bin/mtc-admin create-superadmin admin@example.cz
```

## 6. Služby

```sh
cp /opt/mtcontroller/src/deploy/systemd/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now mtc-web mtc-worker
systemd-analyze security mtc-web   # kontrola sandboxu
```

## 7. nginx a certifikáty

```sh
cp /opt/mtcontroller/src/deploy/nginx/mtcontroller.conf /etc/nginx/sites-available/
ln -s ../sites-available/mtcontroller.conf /etc/nginx/sites-enabled/
# upravit hostname a `allow` admin sítě, pak:
certbot certonly --nginx -d mtc-admin.example.cz -d mtc-checkin.example.cz
nginx -t && systemctl reload nginx
```

## 8. Firewall (nftables)

Z internetu jen 443 na check-in, SSH a admin UI jen z admin sítě.
Statická routa na tunelový pool přes koncentrátor:

```sh
ip route add 10.200.0.0/16 via <IP koncentrátoru>   # trvale v /etc/network/interfaces
```
