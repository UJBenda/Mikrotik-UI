#!/bin/sh
# Ověření schématu proti skutečnému Postgresu (16+).
# Použití (jako uživatel s právem superuser, typicky postgres):
#   tests/db/run.sh [jméno_testovací_db]
# Vytvoří role mtc_owner / mtc_app (pokud chybí), testovací DB, aplikuje
# migraci, spustí kontroly, otestuje downgrade a DB smaže.
set -eu
DB="${1:-mtc_schema_test}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SQL="$HERE/../../migrations/sql"

psql -qX -v ON_ERROR_STOP=1 -d postgres <<EOF
SELECT 'CREATE ROLE mtc_owner LOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='mtc_owner') \gexec
SELECT 'CREATE ROLE mtc_app LOGIN NOBYPASSRLS' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='mtc_app') \gexec
DROP DATABASE IF EXISTS $DB;
CREATE DATABASE $DB OWNER mtc_owner;
EOF
psql -qX -v ON_ERROR_STOP=1 -d "$DB" -c 'REVOKE ALL ON SCHEMA public FROM PUBLIC' -c 'ALTER SCHEMA public OWNER TO mtc_owner'

apply() {
  { echo 'SET ROLE mtc_owner;'; sed 's/{app_role}/mtc_app/g' "$SQL/$1"; } \
    | psql -qX -v ON_ERROR_STOP=1 -d "$DB"
}

apply 0001_initial.up.sql
psql -qX -v ON_ERROR_STOP=1 -d "$DB" -f "$HERE/checks.sql"
apply 0001_initial.down.sql
apply 0001_initial.up.sql   # upgrade po downgradu musí projít znovu
echo "downgrade/upgrade OK"

psql -qX -d postgres -c "DROP DATABASE $DB"
