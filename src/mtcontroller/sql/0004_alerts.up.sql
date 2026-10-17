-- 0004: alerty (otevření, trvání, automatické uzavření) a stav notifikací.

CREATE TYPE alert_severity AS ENUM ('info', 'warning', 'critical');

CREATE TABLE alert (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id       uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    device_id       uuid NOT NULL,
    kind            varchar(50) NOT NULL CHECK (kind ~ '^[a-z_]+$'),
    subject         varchar(200) NOT NULL DEFAULT '',
    severity        alert_severity NOT NULL,
    message         text NOT NULL,
    opened_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_at    timestamptz NOT NULL DEFAULT now(),
    resolved_at     timestamptz,
    acknowledged_by uuid REFERENCES app_user(id),
    acknowledged_at timestamptz,
    notified_open   boolean NOT NULL DEFAULT false,
    notified_close  boolean NOT NULL DEFAULT false,
    notify_attempts integer NOT NULL DEFAULT 0,
    FOREIGN KEY (device_id, tenant_id) REFERENCES device(id, tenant_id) ON DELETE CASCADE
);
-- pro jeden klíč je otevřený vždy nejvýš jeden alert
CREATE UNIQUE INDEX alert_open_key ON alert (device_id, kind, subject) WHERE resolved_at IS NULL;
CREATE INDEX alert_tenant_open ON alert (tenant_id, opened_at DESC) WHERE resolved_at IS NULL;

ALTER TABLE alert ENABLE ROW LEVEL SECURITY;
ALTER TABLE alert FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON alert USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());

GRANT SELECT, INSERT, UPDATE ON alert TO {app_role};
