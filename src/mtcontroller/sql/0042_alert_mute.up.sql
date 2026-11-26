-- Ztlumení upozornění na čas (alertmute.py): pravidlo, ne příznak na alertu – platí i pro alert, který se
-- mezitím zavře a znovu otevře (kmitající port). device_id NULL = všechna zařízení, subject NULL = všechny předměty.
CREATE TABLE alert_mute (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id  uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    device_id  uuid,
    kind       varchar(50) NOT NULL CHECK (kind ~ '^[a-z_]+$'),
    subject    varchar(200),
    until      timestamptz NOT NULL,
    created_by uuid REFERENCES app_user(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (device_id, tenant_id) REFERENCES device(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX alert_mute_lookup ON alert_mute (tenant_id, kind, until);

ALTER TABLE alert_mute ENABLE ROW LEVEL SECURITY;
ALTER TABLE alert_mute FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON alert_mute USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());

GRANT SELECT, INSERT, UPDATE, DELETE ON alert_mute TO {app_role};
