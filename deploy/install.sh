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
  python3-cryptography postgresql nginx openssl ca-certificates
[ -n "$CERTBOT" ] && apt-get install -y -q certbot python3-certbot-nginx
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python >= 3.11"'

say "Uživatel a adresáře"
id mtcontroller >/dev/null 2>&1 || adduser --system --group --home "$DATA" --no-create-home mtcontroller
install -d -o root -g mtcontroller -m 0750 "$ETC"
install -d -o mtcontroller -g mtcontroller -m 0700 "$DATA"
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
if [ ! -f "$ETC/master.key" ]; then
  umask 027
  mtc-admin gen-master-key > "$ETC/master.key"
  echo "vytvořen master klíč $ETC/master.key – ZÁLOHUJ HO OFFLINE"
fi
for f in vpn1 vpn2; do
  if [ ! -f "$ETC/$f.password" ]; then
    umask 027
    python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > "$ETC/$f.password"
  fi
done
chown root:mtcontroller "$ETC"/config.toml "$ETC"/master.key "$ETC"/*.password
chmod 0640 "$ETC"/config.toml "$ETC"/master.key "$ETC"/*.password

say "PostgreSQL"
PGVER=$(ls /etc/postgresql | sort -V | tail -1)
PGETC=/etc/postgresql/$PGVER/main
runuser -u postgres -- psql -qX -v ON_ERROR_STOP=1 <<'EOF'
SELECT 'CREATE ROLE mtc_owner LOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mtc_owner') \gexec
SELECT 'CREATE ROLE mtc_app LOGIN NOBYPASSRLS' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mtc_app') \gexec
SELECT 'CREATE DATABASE mtcontroller OWNER mtc_owner' WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'mtcontroller') \gexec
EOF
runuser -u postgres -- psql -qX -d mtcontroller -c 'REVOKE ALL ON SCHEMA public FROM PUBLIC' -c 'ALTER SCHEMA public OWNER TO mtc_owner'
# MikroTik controller: služba -> mtc_app; root -> mtc_owner (migrace) i mtc_app
# (ostatní příkazy mtc-admin běží rolí aplikace, tedy s RLS)
grep -q 'MikroTik controller' "$PGETC/pg_ident.conf" || \
  echo '# MikroTik controller: sluzba -> mtc_app, root -> mtc_owner (migrace) a mtc_app (mtc-admin)' >> "$PGETC/pg_ident.conf"
for m in "mtcontroller mtc_app" "root mtc_owner" "root mtc_app"; do
  set -- $m
  grep -Eq "^mtc[[:space:]]+$1[[:space:]]+$2[[:space:]]*\$" "$PGETC/pg_ident.conf" || \
    printf 'mtc   %-14s %s\n' "$1" "$2" >> "$PGETC/pg_ident.conf"
done
if ! grep -q 'map=mtc' "$PGETC/pg_hba.conf"; then
  sed -i '0,/^local/s//local   mtcontroller   mtc_app,mtc_owner   peer map=mtc\n&/' "$PGETC/pg_hba.conf"
fi
systemctl reload postgresql

say "Migrace"
mtc-admin migrate

say "systemd"
install -m 0644 "$SRC/deploy/systemd/mtc-web.service" "$SRC/deploy/systemd/mtc-worker.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable mtc-web mtc-worker >/dev/null
systemctl restart mtc-web mtc-worker
sleep 2
systemctl --no-pager --lines=5 status mtc-web mtc-worker || true

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
  1. doplň [[tunnel.concentrators]] v $ETC/config.toml a restartuj: systemctl restart mtc-web mtc-worker
  2. první admin:            mtc-admin create-superadmin admin@...
  3. skript pro koncentrátor: mtc-admin concentrator-script vpn1
  4. kontrola celé instalace: mtc-admin doctor
  5. UI: https://$H_ADMIN/  (z admin sítě $NET)
Zdravotní kontrola: curl -sk --resolve $H_CHECKIN:443:127.0.0.1 https://$H_CHECKIN/healthz
EOF
