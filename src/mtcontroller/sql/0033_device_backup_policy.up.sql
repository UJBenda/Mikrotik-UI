-- Zálohy konfigurace zařízení: výběr zařízení, společná pravidla zákazníka (jak často, kolik verzí držet)
-- a poslední chyba zálohy u zařízení. Výchozí hodnoty odpovídají dosavadnímu chování (denně, ~rok zpět).
ALTER TABLE device
    ADD COLUMN backup_enabled boolean NOT NULL DEFAULT true,
    ADD COLUMN backup_error   varchar(300),
    ADD COLUMN backup_retry_at timestamptz;      -- po chybě: další pokus nejdřív v tento čas

CREATE TABLE device_backup_policy (
    tenant_id    uuid PRIMARY KEY REFERENCES tenant(id) ON DELETE CASCADE,
    mode         varchar(10) NOT NULL DEFAULT 'custom' CHECK (mode IN ('follow', 'custom')),
    every_hours  integer NOT NULL DEFAULT 24 CHECK (every_hours BETWEEN 1 AND 720),
    only_changes boolean NOT NULL DEFAULT false,
    keep         integer NOT NULL DEFAULT 365 CHECK (keep BETWEEN 1 AND 365),
    keep_weekly  integer NOT NULL DEFAULT 0 CHECK (keep_weekly BETWEEN 0 AND 104),
    keep_monthly integer NOT NULL DEFAULT 0 CHECK (keep_monthly BETWEEN 0 AND 120),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE device_backup_policy ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_backup_policy FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON device_backup_policy USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());
GRANT SELECT, INSERT, UPDATE, DELETE ON device_backup_policy TO {app_role};
