# Společné kroky instalace (deploy/install.sh i postinst balíčku .deb). POSIX sh, spouští root.
# Proměnné: ETC, DATA, MTC_ADMIN (cesta k mtc-admin).

mtc_user_dirs() {
  id mtcontroller >/dev/null 2>&1 || adduser --system --group --home "$DATA" --no-create-home mtcontroller
  install -d -o root -g mtcontroller -m 0750 "$ETC"
  install -d -o mtcontroller -g mtcontroller -m 0700 "$DATA"
}

mtc_master_key() {
  if [ ! -f "$ETC/master.key" ]; then
    (umask 027; "$MTC_ADMIN" gen-master-key > "$ETC/master.key")
    echo "vytvořen master klíč $ETC/master.key – ZÁLOHUJ HO OFFLINE"
  fi
  chown root:mtcontroller "$ETC/master.key"
  chmod 0640 "$ETC/master.key"
}

mtc_postgres() {
  PGVER=$(ls /etc/postgresql | sort -V | tail -1)
  PGETC=/etc/postgresql/$PGVER/main
  runuser -u postgres -- psql -qX -v ON_ERROR_STOP=1 <<'EOSQL'
SELECT 'CREATE ROLE mtc_owner LOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mtc_owner') \gexec
SELECT 'CREATE ROLE mtc_app LOGIN NOBYPASSRLS' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mtc_app') \gexec
SELECT 'CREATE DATABASE mtcontroller OWNER mtc_owner' WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'mtcontroller') \gexec
EOSQL
  runuser -u postgres -- psql -qX -d mtcontroller -c 'REVOKE ALL ON SCHEMA public FROM PUBLIC' -c 'ALTER SCHEMA public OWNER TO mtc_owner'
  # služba -> mtc_app; root -> mtc_owner (migrace) i mtc_app (ostatní příkazy mtc-admin, s RLS)
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
}

mtc_sysctl() {
  # ICMP redirecty: koncentrátor je pošle, když tunel zařízení zrovna neběží – kernel by si pak
  # cachoval routu na pool přes výchozí bránu a správa do tunelu by přestala chodit
  cat > /etc/sysctl.d/99-mtcontroller.conf <<'EOSYS'
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv4.conf.all.secure_redirects = 0
EOSYS
  sysctl -q --system >/dev/null 2>&1 || true
  ip route flush cache 2>/dev/null || true
}

# Certifikáty HTTPS: nginx musí naběhnout vždy – sloty bez certifikátu dostanou certifikát
# z CA controlleru (Let's Encrypt / vlastní se nastaví potom: mtc-admin tls … nebo v UI).
# Obnovy Let's Encrypt přenese do slotů deploy hook certbotu.
mtc_tls_hook() {
  install -d -m 0750 /etc/ssl/mtc
  install -d -m 0755 /etc/ssl/mtc-public /var/www/html /etc/letsencrypt/renewal-hooks/deploy
  printf '#!/bin/sh\n# MikroTik Controller: obnoveny certifikat do /etc/ssl/mtc a prenacteni nginx\nexec %s tls-sync\n' \
    "$MTC_ADMIN" > /etc/letsencrypt/renewal-hooks/deploy/mtcontroller
  chmod 0755 /etc/letsencrypt/renewal-hooks/deploy/mtcontroller
}
