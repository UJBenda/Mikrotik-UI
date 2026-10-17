# Pokyny pro práci v repu

- Komunikace a komentáře v kódu česky, identifikátory anglicky.
- Návrh je v `docs/architecture.md`; větší změny architektury do něj zapsat.
- Cíl běhu: nativní Debian, systemd, bez Dockeru. RouterOS jen v7 (REST API).
- Schéma DB se mění jen migracemi. SQL je v `migrations/sql/NNNN_*.up.sql`
  a `.down.sql`, Alembic revize ho jen načítá. `models.py` musí schématu odpovídat.
- Každá nová tabulka s daty tenanta: sloupec `tenant_id`, `ENABLE` + `FORCE ROW
  LEVEL SECURITY`, politika přes `mtc_current_tenant()` / `mtc_is_operator()`,
  GRANT pro `{app_role}`, a kontrola v `tests/db/checks.sql`.
- Přístup k datům tenanta jen přes `db.tenant_scope()`; `operator_scope()` jen
  pro operace provozovatele.
- Tajemství nikdy v plaintextu v DB ani v logu: `security.SecretBox` s kontextem
  vázaným na záznam.
- Každá akce měnící stav zapisuje `audit_log`.
- Testy: `pytest` a `sudo -u postgres tests/db/run.sh`.
