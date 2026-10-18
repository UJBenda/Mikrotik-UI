-- 0012: poznámky ke klientům (podle MAC) – jen v controlleru, na zařízení se nepíší.

CREATE TABLE client_note (
    tenant_id  uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    mac        varchar(17) NOT NULL CHECK (mac ~ '^[0-9A-F]{2}(:[0-9A-F]{2}){5}$'),
    note       varchar(500) NOT NULL,
    updated_by uuid REFERENCES app_user(id),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, mac)
);

ALTER TABLE client_note ENABLE ROW LEVEL SECURITY;
ALTER TABLE client_note FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON client_note USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());

GRANT SELECT, INSERT, UPDATE, DELETE ON client_note TO {app_role};
