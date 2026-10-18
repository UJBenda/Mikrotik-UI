-- 0013: řízený režim (F5a). Záměr lokality (sítě, management, výchozí přístupová síť)
-- je v site_policy (module 'intent'); tady jsou výjimky per zařízení a příznak převzetí.

-- výjimky zařízení: role portů a adresa managementu (prázdné = výchozí ze záměru lokality)
CREATE TABLE device_intent (
    tenant_id  uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    device_id  uuid NOT NULL,
    ports      jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(ports) = 'object'),
    mgmt       jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(mgmt) = 'object'),
    updated_by uuid REFERENCES app_user(id),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id),
    FOREIGN KEY (device_id, tenant_id) REFERENCES device(id, tenant_id) ON DELETE CASCADE
);

ALTER TABLE device_intent ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_intent FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON device_intent USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());

GRANT SELECT, INSERT, UPDATE, DELETE ON device_intent TO {app_role};

-- heslo zálohy pořízené na zařízení těsně před převzetím (návrat = backup load)
ALTER TYPE secret_kind ADD VALUE IF NOT EXISTS 'takeover_backup';

-- čas převzetí do řízení (NULL = zařízení je jen sledované)
ALTER TABLE device ADD COLUMN managed_at timestamptz;
