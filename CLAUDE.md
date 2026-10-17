# Pokyny pro práci v repu

- Komunikace a komentáře v kódu česky, identifikátory anglicky.
- Návrh je v `docs/architecture.md`; větší změny architektury do něj zapsat.
- Cíl běhu: nativní Debian, systemd, bez Dockeru. RouterOS jen v7 (REST API).
- Stack jen z Debian apt: Starlette, uvicorn, psycopg, pydantic, cryptography.
  Nepřidávat závislosti, které v Debianu 13 nejsou.
- Schéma DB se mění jen migracemi `src/mtcontroller/sql/NNNN_*.up.sql` + `.down.sql`.
  SQL parametry `%(x)s`, inet/cidr vybírat jako text.
- Každá nová tabulka s daty tenanta: sloupec `tenant_id`, `ENABLE` + `FORCE ROW
  LEVEL SECURITY`, politika přes `mtc_current_tenant()` / `mtc_is_operator()`,
  GRANT pro `{app_role}`, a kontrola v `tests/db/checks.sql`.
- Přístup k datům tenanta jen přes `db.tenant_scope()`; `operator_scope()` jen
  pro operace provozovatele a vyhledání podle tokenu/klíče.
- Zápis auditu u neúspěchu (špatné heslo apod.) commitnout dřív, než se vyhodí
  výjimka – jinak ho rollback smaže.
- RouterOS skripty: jen přes `routeros.q()`, čisté ASCII, obalené `{ }`, vše co
  controller vytvoří má jméno/komentář `mtc`. Nové skripty přidat do testů v `test_routeros.py`.
- Tajemství nikdy v plaintextu v DB ani v logu: `security.SecretBox` s kontextem
  vázaným na záznam.
- Každá akce měnící stav zapisuje `audit_log`.
- Testy: `python3 -m pytest` a `sudo -u postgres tests/db/run.sh`.
