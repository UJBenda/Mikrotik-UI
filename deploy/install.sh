#!/bin/sh
# Instalace / aktualizace MikroTik Controlleru na Debian 13 (trixie).
# Vše z apt, žádné PyPI. Skript je idempotentní – spouští se i pro aktualizaci.
#
#   sudo ./deploy/install.sh --admin-host mtc-admin.firma.cz \
#        --checkin-host mtc-checkin.firma.cz --controller-ip 10.10.0.10 \
#        --admin-net 10.10.0.0/24 [--lab] [--certbot admin@firma.cz]
#
#   --lab       laboratoř: self-signed certifikáty pro nginx a vypnuté ověřování
#               certifikátů na MikroTicích (device_check_certificate = "no").
#               NIKDY v produkci.
#   --certbot   získá certifikáty Let's Encrypt (DNS obou hostname musí mířit sem).
set -eu

ADMIN_HOST="" CHECKIN_HOST="" CONTROLLER_IP="" ADMIN_NET="" LAB=0 CERTBOT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --admin-host) ADMIN_HOST="$2"; shift 2 ;;
    --checkin-host) CHECKIN_HOST="$2"; shift 2 ;;
    --controller-ip) CONTROLLER_IP="$2"; shift 2 ;;
    --admin-net) ADMIN_NET="$2"; shift 2 ;;
    --lab) LAB=1; shift ;;
    --certbot) CERTBOT="$2"; shift 2 ;;
    *) echo "neznámý parametr: $1" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" = 0 ] || { echo "spusť jako root" >&2; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/mtcontroller/app
ETC=/etc/mtcontroller
DATA=/var/lib/mtcontroller

say() { printf '\n==> %s\n' "$*"; }

say "Balíčky (apt)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 python3-starlette python3-uvicorn python3-psycopg python3-pydantic \
  python3-cryptography postgresql nginx openssl ca-certificates ieee-data
[ -n "$CERTBOT" ] && apt-get install -y -q certbot python3-certbot-nginx
python3 -c 'import sys; assert sys.version_info >= (3, 12), "Python >= 3.12"'
python3 -c 'import pydantic; assert int(pydantic.VERSION.split(".")[0]) >= 2, "pydantic >= 2 (Debian 13; Ubuntu 24.04 má jen 1.10)"' 

say "Uživatel a adresáře"
MTC_ADMIN=/usr/local/sbin/mtc-admin
. "$SRC/deploy/common.sh"
mtc_user_dirs
install -d -o root -g root -m 0755 /opt/mtcontroller

say "Aplikace → $APP"
rm -rf "$APP.new"
install -d -m 0755 "$APP.new"
cp -a "$SRC/src/mtcontroller" "$APP.new/"
find "$APP.new" -name __pycache__ -prune -exec rm -rf {} +
chown -R root:root "$APP.new"
chmod -R u=rwX,go=rX "$APP.new"
rm -rf "$APP.old"
[ -d "$APP" ] && mv "$APP" "$APP.old"
mv "$APP.new" "$APP"
git -C "$SRC" rev-parse --short HEAD > "$APP/VERSION" 2>/dev/null || true

cat > /usr/local/sbin/mtc-admin <<EOF
#!/bin/sh
exec env PYTHONPATH=$APP MTC_CONFIG=$ETC/config.toml python3 -m mtcontroller.cli "\$@"
EOF
chmod 0755 /usr/local/sbin/mtc-admin

say "Konfigurace"
if [ ! -f "$ETC/config.toml" ]; then
  [ -n "$ADMIN_HOST" ] && [ -n "$CHECKIN_HOST" ] && [ -n "$CONTROLLER_IP" ] || {
    echo "první instalace potřebuje --admin-host, --checkin-host a --controller-ip" >&2; exit 2; }
  sed -e "s/mtc-admin.example.cz/$ADMIN_HOST/" -e "s/mtc-checkin.example.cz/$CHECKIN_HOST/" \
      -e "s/^controller_ip = .*/controller_ip = \"$CONTROLLER_IP\"/" \
      "$SRC/config.toml.example" > "$ETC/config.toml"
  if [ "$LAB" = 1 ]; then
    sed -i 's/^device_check_certificate = .*/device_check_certificate = "no"  # LAB! v produkci "yes-without-crl"/' "$ETC/config.toml"
  fi
  echo "vytvořen $ETC/config.toml – DOPLŇ sekci [[tunnel.concentrators]] (connect_host, api_url)"
else
  echo "$ETC/config.toml existuje, beze změn"
fi
mtc_master_key
for f in vpn1 vpn2; do
  if [ ! -f "$ETC/$f.password" ]; then
    umask 027
    python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > "$ETC/$f.password"
  fi
done
chown root:mtcontroller "$ETC"/config.toml "$ETC"/*.password
chmod 0640 "$ETC"/config.toml "$ETC"/*.password

say "PostgreSQL"
mtc_postgres

say "Migrace"
mtc-admin migrate

say "systemd"
install -m 0644 "$SRC/deploy/systemd/mtc-web.service" "$SRC/deploy/systemd/mtc-worker.service" "$SRC/deploy/systemd/mtc-syslog.service" \
  "$SRC/deploy/systemd/mtc-backup.service" "$SRC/deploy/systemd/mtc-backup.timer" "$SRC/deploy/systemd/mtc-backup.path" /etc/systemd/system/
install -d -o root -g mtcontroller -m 0750 /var/backups/mtcontroller
if [ ! -f "$ETC/backup.key" ]; then
  (umask 077; PYTHONPATH="$APP" python3 -c 'from mtcontroller.sysbackup import new_passphrase; print(new_passphrase())' > "$ETC/backup.key")
  echo "vytvořeno heslo záloh $ETC/backup.key – ULOŽ SI HO (mtc-admin recovery-kit)"
fi
say "sysctl (ignorovat ICMP redirecty)"
mtc_sysctl

# routy na zařízení přes aktivní koncentrátor (tunnel.manage_routes) potřebují CAP_NET_ADMIN
if grep -Eq '^[[:space:]]*manage_routes[[:space:]]*=[[:space:]]*true' "$ETC/config.toml"; then
  install -d -m 0755 /etc/systemd/system/mtc-worker.service.d
  install -m 0644 "$SRC/deploy/systemd/mtc-worker-routes.conf" /etc/systemd/system/mtc-worker.service.d/routes.conf
else
  rm -f /etc/systemd/system/mtc-worker.service.d/routes.conf
fi
systemctl daemon-reload
systemctl enable mtc-web mtc-worker mtc-syslog mtc-backup.timer mtc-backup.path >/dev/null
systemctl start mtc-backup.timer mtc-backup.path
systemctl restart mtc-web mtc-worker mtc-syslog
sleep 2
systemctl --no-pager --lines=5 status mtc-web mtc-worker mtc-syslog || true

say "nginx"
NGX=/etc/nginx/sites-available/mtcontroller.conf
H_ADMIN=$(sed -n 's/^ui_host = "\(.*\)"/\1/p' "$ETC/config.toml")
H_CHECKIN=$(sed -n 's/^checkin_host = "\(.*\)"/\1/p' "$ETC/config.toml")
NET="${ADMIN_NET:-$(sed -n 's/^ *allow \([0-9./]*\);.*mtc-admin-net/\1/p' "$NGX" 2>/dev/null || true)}"
[ -n "$NET" ] || NET=10.10.0.0/24
if [ "$LAB" = 1 ] && [ ! -f /etc/ssl/mtc/admin.crt ]; then
  install -d -m 0750 /etc/ssl/mtc
  for h in admin checkin; do
    if [ $h = admin ]; then host=$H_ADMIN; else host=$H_CHECKIN; fi
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 825 \
      -subj "/CN=$host" -addext "subjectAltName=DNS:$host" \
      -keyout /etc/ssl/mtc/$h.key -out /etc/ssl/mtc/$h.crt 2>/dev/null
  done
  chmod 0640 /etc/ssl/mtc/*.key
fi
if [ "$LAB" = 1 ]; then
  CA="/etc/ssl/mtc/admin.crt"; KA="/etc/ssl/mtc/admin.key"; CC="/etc/ssl/mtc/checkin.crt"; KC="/etc/ssl/mtc/checkin.key"
else
  # jeden certifikát Let's Encrypt pro oba hostname (SAN)
  CA="/etc/letsencrypt/live/mtcontroller/fullchain.pem"; KA="/etc/letsencrypt/live/mtcontroller/privkey.pem"
  CC="$CA"; KC="$KA"
fi
if [ -n "$CERTBOT" ] && [ ! -f "$CC" ]; then
  systemctl stop nginx || true
  certbot certonly --standalone --non-interactive --agree-tos -m "$CERTBOT" \
    -d "$H_ADMIN" -d "$H_CHECKIN" --cert-name mtcontroller \
    --pre-hook "systemctl stop nginx" --post-hook "systemctl start nginx" \
    || echo "!! certbot selhal (DNS/port 80?) – nginx se nastaví, až certifikát bude"
  systemctl start nginx || true
fi
sed -e "s|mtc-admin.example.cz|$H_ADMIN|g" -e "s|mtc-checkin.example.cz|$H_CHECKIN|g" \
    -e "s|10.10.0.0/24;|$NET;|" \
    -e "s|__ADMIN_CERT__|$CA|" -e "s|__ADMIN_KEY__|$KA|" -e "s|__CHECKIN_CERT__|$CC|" -e "s|__CHECKIN_KEY__|$KC|" \
    "$SRC/deploy/nginx/mtcontroller.conf" > "$NGX"
ln -sfn "$NGX" /etc/nginx/sites-enabled/mtcontroller.conf
rm -f /etc/nginx/sites-enabled/default
if [ -f "$CA" ] && [ -f "$CC" ]; then
  nginx -t && systemctl reload nginx
else
  echo "!! chybí certifikáty ($CA, $CC) – nginx nepřenačten. Spusť znovu s --certbot EMAIL nebo --lab."
fi

say "Hotovo"
cat <<EOF
Další kroky (viz docs/test-plan.md):
  1. doplň [[tunnel.concentrators]] v $ETC/config.toml a restartuj: systemctl restart mtc-web mtc-worker mtc-syslog
  2. první admin:            mtc-admin create-superadmin admin@...
  3. skript pro koncentrátor: mtc-admin concentrator-script vpn1
  4. kontrola celé instalace: mtc-admin doctor
  5. UI: https://$H_ADMIN/  (z admin sítě $NET)
Zdravotní kontrola: curl -sk --resolve $H_CHECKIN:443:127.0.0.1 https://$H_CHECKIN/healthz
EOF
