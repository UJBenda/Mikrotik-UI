-- 0015: výchozí hesla ze štítků zařízení (podle sériového čísla), jen šifrovaně
-- (SecretBox, kontext "factory:<tenant>:<serial>"). Zadávají se i před adopcí.

CREATE TABLE factory_password (
    tenant_id   uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    serial      varchar(64) NOT NULL CHECK (serial ~ '^[A-Za-z0-9._-]{3,64}$'),
    wrapped_key bytea NOT NULL,
    ciphertext  bytea NOT NULL,
    key_version integer NOT NULL DEFAULT 1,
    note        varchar(200),
    updated_by  uuid REFERENCES app_user(id),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, serial)
);

ALTER TABLE factory_password ENABLE ROW LEVEL SECURITY;
ALTER TABLE factory_password FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON factory_password USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());

GRANT SELECT, INSERT, UPDATE, DELETE ON factory_password TO {app_role};
