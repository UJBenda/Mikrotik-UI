-- Kontroly schématu: izolace tenantů (RLS), append-only audit,
-- jednorázové adopční tokeny, integritní pravidla.
-- Spouští tests/db/run.sh proti prázdné testovací DB.
\set ON_ERROR_STOP on

-- data připraví provozovatel
SET ROLE mtc_app;
BEGIN;
SELECT set_config('app.operator', 'on', true);
INSERT INTO tenant (id, name, tunnel_block) VALUES
  ('00000000-0000-0000-0000-0000000000a1', 'Firma A', '10.200.1.0/24'),
  ('00000000-0000-0000-0000-0000000000b1', 'Firma B', '10.200.2.0/24');
INSERT INTO site (id, tenant_id, name) VALUES
  ('00000000-0000-0000-0000-00000000a5a1', '00000000-0000-0000-0000-0000000000a1', 'Praha'),
  ('00000000-0000-0000-0000-00000000b5b1', '00000000-0000-0000-0000-0000000000b1', 'Brno');
INSERT INTO device (tenant_id, site_id, serial, state, tunnel_ip) VALUES
  ('00000000-0000-0000-0000-0000000000a1', '00000000-0000-0000-0000-00000000a5a1', 'SER-A1', 'adopted', '10.200.1.1'),
  ('00000000-0000-0000-0000-0000000000b1', '00000000-0000-0000-0000-00000000b5b1', 'SER-B1', 'adopted', '10.200.2.1');
INSERT INTO app_user (id, email, password_hash) VALUES
  ('00000000-0000-0000-0000-0000000000ee', 'admin@example.cz', 'x');
COMMIT;

-- 1) bez kontextu nevidí aplikace nic
DO $$ BEGIN
  IF (SELECT count(*) FROM device) <> 0 THEN
    RAISE EXCEPTION 'FAIL: bez tenant kontextu jsou vidět zařízení';
  END IF;
  RAISE NOTICE 'OK 1 bez kontextu nic';
END $$;

-- 2) tenant A vidí jen svoje
BEGIN;
SELECT set_config('app.tenant_id', '00000000-0000-0000-0000-0000000000a1', true);
DO $$ BEGIN
  IF (SELECT array_agg(serial) FROM device) <> ARRAY['SER-A1']::varchar[] THEN
    RAISE EXCEPTION 'FAIL: tenant A vidí cizí zařízení';
  END IF;
  IF (SELECT count(*) FROM tenant) <> 1 THEN
    RAISE EXCEPTION 'FAIL: tenant A vidí cizího tenanta';
  END IF;
  RAISE NOTICE 'OK 2 tenant vidí jen svoje';
END $$;

-- 3) tenant A nesmí zapsat zařízení do tenanta B
DO $$ BEGIN
  BEGIN
    INSERT INTO device (tenant_id, serial) VALUES ('00000000-0000-0000-0000-0000000000b1', 'EVIL');
    RAISE EXCEPTION 'FAIL: zápis do cizího tenanta prošel';
  EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'OK 3 zápis do cizího tenanta odmítnut';
  END;
END $$;

-- 4) tenant A nesmí cizí zařízení přesunout k sobě ani ho měnit
DO $$
DECLARE n int;
BEGIN
  UPDATE device SET identity = 'x' WHERE serial = 'SER-B1';
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 0 THEN RAISE EXCEPTION 'FAIL: tenant A změnil zařízení tenanta B'; END IF;
  RAISE NOTICE 'OK 4 cizí zařízení nelze měnit';
END $$;

-- 5) zařízení nejde přiřadit k lokalitě jiného tenanta
DO $$ BEGIN
  BEGIN
    UPDATE device SET site_id = '00000000-0000-0000-0000-00000000b5b1' WHERE serial = 'SER-A1';
    RAISE EXCEPTION 'FAIL: zařízení přiřazeno k cizí lokalitě';
  EXCEPTION WHEN foreign_key_violation THEN
    RAISE NOTICE 'OK 5 cizí lokalita odmítnuta';
  END;
END $$;

-- 6) tenant nesmí zakládat tenanty
DO $$ BEGIN
  BEGIN
    INSERT INTO tenant (name, tunnel_block) VALUES ('X', '10.200.9.0/24');
    RAISE EXCEPTION 'FAIL: tenant založil tenanta';
  EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'OK 6 zakládání tenantů jen provozovatel';
  END;
END $$;

-- 7) audit: zápis jen pro vlastního tenanta, změna/mazání zakázáno
INSERT INTO audit_log (tenant_id, action) VALUES ('00000000-0000-0000-0000-0000000000a1', 'test.ok');
DO $$ BEGIN
  BEGIN
    INSERT INTO audit_log (tenant_id, action) VALUES ('00000000-0000-0000-0000-0000000000b1', 'test.evil');
    RAISE EXCEPTION 'FAIL: audit zapsán do cizího tenanta';
  EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'OK 7a audit do cizího tenanta odmítnut';
  END;
  BEGIN
    UPDATE audit_log SET action = 'x';
    RAISE EXCEPTION 'FAIL: audit šel změnit';
  EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'OK 7b UPDATE auditu odmítnut (práva)';
  END;
  BEGIN
    DELETE FROM audit_log;
    RAISE EXCEPTION 'FAIL: audit šel smazat';
  EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'OK 7c DELETE auditu odmítnut (práva)';
  END;
END $$;
COMMIT;

-- 8) audit je append-only i pro vlastníka schématu:
--    a) RLS nemá politiku pro UPDATE/DELETE → vlastník nezmění žádný řádek,
--    b) i kdyby RLS vypnul, zastaví ho trigger.
RESET ROLE;
SET ROLE mtc_owner;
BEGIN;
SELECT set_config('app.operator', 'on', true);
DO $$
DECLARE n int;
BEGIN
  UPDATE audit_log SET action = 'x';
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n <> 0 THEN RAISE EXCEPTION 'FAIL: vlastník změnil audit přes RLS'; END IF;
  RAISE NOTICE 'OK 8a RLS nepustí UPDATE auditu';
END $$;
ALTER TABLE audit_log NO FORCE ROW LEVEL SECURITY;
DO $$ BEGIN
  BEGIN
    DELETE FROM audit_log;
    RAISE EXCEPTION 'FAIL: vlastník smazal audit bez RLS';
  EXCEPTION WHEN raise_exception THEN
    IF SQLERRM LIKE 'FAIL%' THEN RAISE; END IF;
    RAISE NOTICE 'OK 8b trigger zastaví DELETE i bez RLS';
  END;
END $$;
ROLLBACK;

-- 9) adopční token jen jednou
RESET ROLE;
SET ROLE mtc_app;
BEGIN;
SELECT set_config('app.tenant_id', '00000000-0000-0000-0000-0000000000a1', true);
INSERT INTO adoption_token (id, tenant_id, token_hash, created_by, expires_at)
VALUES ('00000000-0000-0000-0000-00000000070c', '00000000-0000-0000-0000-0000000000a1',
        repeat('a', 64), '00000000-0000-0000-0000-0000000000ee', now() + interval '1 hour');
UPDATE adoption_token SET used_at = now(),
  used_by_device = (SELECT id FROM device WHERE serial = 'SER-A1')
  WHERE id = '00000000-0000-0000-0000-00000000070c';
DO $$ BEGIN
  BEGIN
    UPDATE adoption_token SET used_at = now() WHERE id = '00000000-0000-0000-0000-00000000070c';
    RAISE EXCEPTION 'FAIL: token použit dvakrát';
  EXCEPTION WHEN raise_exception THEN
    IF SQLERRM LIKE 'FAIL%' THEN RAISE; END IF;
    RAISE NOTICE 'OK 9 token jen jednou';
  END;
END $$;

-- 10) adoptované zařízení musí mít tunelovou IP a lokalitu
DO $$ BEGIN
  BEGIN
    INSERT INTO device (tenant_id, serial, state) VALUES ('00000000-0000-0000-0000-0000000000a1', 'SER-A2', 'adopted');
    RAISE EXCEPTION 'FAIL: adoptované zařízení bez IP prošlo';
  EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'OK 10 adoptované zařízení vyžaduje IP a lokalitu';
  END;
END $$;
COMMIT;

-- 12) fronta příkazů: tenant nevidí ani nevloží příkaz pro cizí zařízení
BEGIN;
SELECT set_config('app.operator', 'on', true);
INSERT INTO device_command (tenant_id, device_id, kind)
SELECT tenant_id, id, 'reboot' FROM device WHERE serial = 'SER-B1';
COMMIT;
BEGIN;
SELECT set_config('app.tenant_id', '00000000-0000-0000-0000-0000000000a1', true);
DO $$ BEGIN
  IF (SELECT count(*) FROM device_command) <> 0 THEN
    RAISE EXCEPTION 'FAIL: tenant A vidí příkazy tenanta B';
  END IF;
  BEGIN
    INSERT INTO device_command (tenant_id, device_id, kind)
    SELECT '00000000-0000-0000-0000-0000000000a1', id, 'reboot' FROM device WHERE serial = 'SER-B1';
    -- SELECT z device pod RLS nic nevrátí, takže nic nevloží; zkusíme i přímo:
    INSERT INTO device_command (tenant_id, device_id, kind)
    VALUES ('00000000-0000-0000-0000-0000000000b1',
            (SELECT id FROM device LIMIT 1), 'reboot');
    RAISE EXCEPTION 'FAIL: příkaz do cizího tenanta prošel';
  EXCEPTION WHEN insufficient_privilege OR not_null_violation THEN
    RAISE NOTICE 'OK 12 fronta příkazů izolovaná';
  END;
END $$;
COMMIT;

-- 13) inventura, zálohy a plány: izolace tenantů
BEGIN;
SELECT set_config('app.operator', 'on', true);
INSERT INTO device_inventory (tenant_id, device_id, section, data)
SELECT tenant_id, id, 'resource', '{"x":1}' FROM device WHERE serial = 'SER-B1';
INSERT INTO device_backup (tenant_id, device_id, size_bytes, sha256, content_sha, changed, wrapped_key, ciphertext)
SELECT tenant_id, id, 1, repeat('a',64), repeat('a',64), true, '\x00', '\x00' FROM device WHERE serial = 'SER-B1';
COMMIT;
BEGIN;
SELECT set_config('app.tenant_id', '00000000-0000-0000-0000-0000000000a1', true);
DO $$ BEGIN
  IF (SELECT count(*) FROM device_inventory) + (SELECT count(*) FROM device_backup) <> 0 THEN
    RAISE EXCEPTION 'FAIL: tenant A vidí inventuru nebo zálohy tenanta B';
  END IF;
  BEGIN
    INSERT INTO config_plan (tenant_id, device_id, module, summary, changes, script, basis_sha)
    VALUES ('00000000-0000-0000-0000-0000000000b1',
            (SELECT id FROM device LIMIT 1), 'ntp', 'x', '{}', '', repeat('a',64));
    RAISE EXCEPTION 'FAIL: plán do cizího tenanta prošel';
  EXCEPTION WHEN insufficient_privilege OR not_null_violation THEN
    RAISE NOTICE 'OK 13 inventura, zálohy a plány izolované';
  END;
  BEGIN
    UPDATE device_backup SET size_bytes = 2;
    RAISE EXCEPTION 'FAIL: zálohu šlo upravit';
  EXCEPTION WHEN insufficient_privilege THEN
    RAISE NOTICE 'OK 13b zálohy nejdou upravovat';
  END;
END $$;
COMMIT;

-- 14) alerty: izolace tenantů, jeden otevřený alert na klíč
BEGIN;
SELECT set_config('app.operator', 'on', true);
INSERT INTO alert (tenant_id, device_id, kind, severity, message)
SELECT tenant_id, id, 'offline', 'critical', 'x' FROM device WHERE serial = 'SER-B1';
DO $$ BEGIN
  BEGIN
    INSERT INTO alert (tenant_id, device_id, kind, severity, message)
    SELECT tenant_id, id, 'offline', 'critical', 'y' FROM device WHERE serial = 'SER-B1';
    RAISE EXCEPTION 'FAIL: dva otevřené alerty se stejným klíčem';
  EXCEPTION WHEN unique_violation THEN
    RAISE NOTICE 'OK 14a jeden otevřený alert na klíč';
  END;
END $$;
COMMIT;
BEGIN;
SELECT set_config('app.tenant_id', '00000000-0000-0000-0000-0000000000a1', true);
DO $$ BEGIN
  IF (SELECT count(*) FROM alert) <> 0 THEN RAISE EXCEPTION 'FAIL: tenant A vidí alerty tenanta B'; END IF;
  RAISE NOTICE 'OK 14b alerty izolované';
END $$;
COMMIT;

-- 11) superadmin role musí být globální
BEGIN;
DO $$ BEGIN
  BEGIN
    INSERT INTO role_binding (user_id, role, tenant_id)
    VALUES ('00000000-0000-0000-0000-0000000000ee', 'superadmin', '00000000-0000-0000-0000-0000000000a1');
    RAISE EXCEPTION 'FAIL: superadmin vázaný na tenanta';
  EXCEPTION WHEN check_violation THEN
    RAISE NOTICE 'OK 11 superadmin jen globálně';
  END;
END $$;
COMMIT;

-- 15) changelogy RouterOS: číst smí každý, zapisovat jen provozovatel
BEGIN;
SELECT set_config('app.operator', 'on', true);
INSERT INTO ros_release (version, changelog) VALUES ('7.20.5', 'x');
COMMIT;
BEGIN;
SELECT set_config('app.tenant_id', '00000000-0000-0000-0000-0000000000a1', true);
DO $$ BEGIN
  IF (SELECT count(*) FROM ros_release) <> 1 THEN RAISE EXCEPTION 'FAIL: tenant nevidí changelog'; END IF;
  BEGIN
    INSERT INTO ros_release (version, changelog) VALUES ('7.20.6', 'evil');
    RAISE EXCEPTION 'FAIL: tenant zapsal changelog';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
  UPDATE ros_release SET changelog = 'evil';
  IF (SELECT changelog FROM ros_release WHERE version = '7.20.5') <> 'x' THEN
    RAISE EXCEPTION 'FAIL: tenant změnil changelog';
  END IF;
  RAISE NOTICE 'OK 15 changelogy čte každý, zapisuje jen provozovatel';
END $$;
COMMIT;

-- 16) postupné aktualizace izolované mezi tenanty
BEGIN;
SELECT set_config('app.operator', 'on', true);
INSERT INTO upgrade_job (tenant_id, site_id, steps)
SELECT tenant_id, id, '[]' FROM site WHERE tenant_id = '00000000-0000-0000-0000-0000000000b1' LIMIT 1;
COMMIT;
BEGIN;
SELECT set_config('app.tenant_id', '00000000-0000-0000-0000-0000000000a1', true);
DO $$ BEGIN
  IF (SELECT count(*) FROM upgrade_job) <> 0 THEN RAISE EXCEPTION 'FAIL: tenant A vidí úlohy tenanta B'; END IF;
  BEGIN
    INSERT INTO upgrade_job (tenant_id, site_id, steps)
    SELECT '00000000-0000-0000-0000-0000000000b1', id, '[]' FROM site LIMIT 1;
    RAISE EXCEPTION 'FAIL: tenant A založil úlohu tenantovi B';
  EXCEPTION WHEN insufficient_privilege THEN NULL;
  END;
  RAISE NOTICE 'OK 16 postupné aktualizace izolované';
END $$;
COMMIT;

RESET ROLE;
\echo ALL CHECKS PASSED
