#!/bin/sh
# Ověření schématu proti skutečnému Postgresu (16+).
# Použití (jako superuser, typicky postgres):  tests/db/run.sh [testovaci_db]
# Vytvoří role mtc_owner / mtc_app (pokud chybí) a testovací DB, aplikuje
# všechny migrace, spustí kontroly, otestuje downgrade + upgrade a DB smaže.
set -eu
DB="${1:-mtc_schema_test}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SQL="$HERE/../../src/mtcontroller/sql"

psql -qX -v ON_ERROR_STOP=1 -d postgres <<EOF
SELECT 'CREATE ROLE mtc_owner LOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='mtc_owner') \gexec
SELECT 'CREATE ROLE mtc_app LOGIN NOBYPASSRLS' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='mtc_app') \gexec
DROP DATABASE IF EXISTS $DB;
CREATE DATABASE $DB OWNER mtc_owner;
EOF
psql -qX -v ON_ERROR_STOP=1 -d "$DB" -c 'REVOKE ALL ON SCHEMA public FROM PUBLIC' -c 'ALTER SCHEMA public OWNER TO mtc_owner'

apply() {
  { echo 'SET ROLE mtc_owner;'; echo 'BEGIN;'; sed 's/{app_role}/mtc_app/g' "$1"; echo 'COMMIT;'; } \
    | psql -qX -v ON_ERROR_STOP=1 -d "$DB"
}

UPS=$(ls "$SQL"/[0-9][0-9][0-9][0-9]_*.up.sql | sort)
DOWNS=$(ls "$SQL"/[0-9][0-9][0-9][0-9]_*.down.sql | sort -r)

for f in $UPS; do apply "$f"; done
psql -qX -v ON_ERROR_STOP=1 -d "$DB" -f "$HERE/checks.sql"
for f in $DOWNS; do apply "$f"; done
for f in $UPS; do apply "$f"; done   # upgrade po downgradu musí projít znovu
echo "downgrade/upgrade OK"

psql -qX -d postgres -c "DROP DATABASE $DB"
