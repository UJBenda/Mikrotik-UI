-- Nastavení provozovatele (plán záloh controlleru) a cíle záloh v cloudu.
-- Nejsou to data tenanta: jen provozovatel (superadmin / root), tenant nevidí nic.

CREATE TABLE system_setting (
    key        varchar(50) PRIMARY KEY CHECK (key ~ '^[a-z_]+$'),
    data       jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE system_setting ENABLE ROW LEVEL SECURITY;
ALTER TABLE system_setting FORCE ROW LEVEL SECURITY;
CREATE POLICY operator_only ON system_setting USING (mtc_is_operator()) WITH CHECK (mtc_is_operator());
GRANT SELECT, INSERT, UPDATE, DELETE ON system_setting TO {app_role};

-- Kam se zálohy kopírují (Nextcloud WebDAV, Google Drive, OneDrive). Hesla / refresh tokeny
-- jen šifrovaně (SecretBox, kontext backup_target:<id>); záloha sama je šifrovaná už u nás.
CREATE TABLE backup_target (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind        varchar(20) NOT NULL CHECK (kind IN ('nextcloud', 'gdrive', 'onedrive')),
    name        varchar(80) NOT NULL,
    enabled     boolean NOT NULL DEFAULT true,
    keep        integer NOT NULL DEFAULT 14 CHECK (keep BETWEEN 1 AND 365),
    config      jsonb NOT NULL DEFAULT '{}',
    wrapped_key bytea,
    ciphertext  bytea,
    key_version integer NOT NULL DEFAULT 1,
    status      jsonb NOT NULL DEFAULT '{}',
    created_by  uuid REFERENCES app_user(id) ON DELETE SET NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CHECK ((wrapped_key IS NULL) = (ciphertext IS NULL))
);
ALTER TABLE backup_target ENABLE ROW LEVEL SECURITY;
ALTER TABLE backup_target FORCE ROW LEVEL SECURITY;
CREATE POLICY operator_only ON backup_target USING (mtc_is_operator()) WITH CHECK (mtc_is_operator());
GRANT SELECT, INSERT, UPDATE, DELETE ON backup_target TO {app_role};
