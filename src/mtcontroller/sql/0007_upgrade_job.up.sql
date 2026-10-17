-- 0007: postupná aktualizace lokality (rolling upgrade). Kroky v jsonb:
-- [{device_id, label, version, status: pending|running|done|skipped|failed, command_id, started_at, message}]

CREATE TYPE upgrade_job_status AS ENUM ('running', 'done', 'stopped', 'cancelled');

CREATE TABLE upgrade_job (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id   uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    site_id     uuid NOT NULL,
    status      upgrade_job_status NOT NULL DEFAULT 'running',
    steps       jsonb NOT NULL CHECK (jsonb_typeof(steps) = 'array'),
    created_by  uuid REFERENCES app_user(id),
    created_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    message     text,
    FOREIGN KEY (site_id, tenant_id) REFERENCES site(id, tenant_id) ON DELETE CASCADE
);
-- v lokalitě běží nejvýš jedna postupná aktualizace
CREATE UNIQUE INDEX upgrade_job_running ON upgrade_job (site_id) WHERE status = 'running';

ALTER TABLE upgrade_job ENABLE ROW LEVEL SECURITY;
ALTER TABLE upgrade_job FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON upgrade_job USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());

GRANT SELECT, INSERT, UPDATE ON upgrade_job TO {app_role};
