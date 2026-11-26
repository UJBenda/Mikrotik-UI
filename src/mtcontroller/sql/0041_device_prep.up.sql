-- Příprava zařízení před nasazením (prep.py): návrh portů, VLAN a správy pro kus, který ještě není ve správě.
-- Z návrhu se generuje skript; až se zařízení na lokalitě ohlásí, spáruje se s návrhem (device_id).
CREATE TABLE device_prep (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id  uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    site_id    uuid NOT NULL,
    name       varchar(60) NOT NULL,
    model      varchar(80) NOT NULL,
    serial     varchar(64),
    data       jsonb NOT NULL,
    status     varchar(16) NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'ready', 'deployed')),
    device_id  uuid,
    created_by uuid REFERENCES app_user(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (site_id, tenant_id) REFERENCES site(id, tenant_id) ON DELETE CASCADE,
    UNIQUE (site_id, name)
);
CREATE INDEX device_prep_site ON device_prep (site_id);

ALTER TABLE device_prep ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_prep FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON device_prep USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());

GRANT SELECT, INSERT, UPDATE, DELETE ON device_prep TO {app_role};
