-- Hodinové průměry vytížení zařízení (CPU, paměť): podklad pro „obvyklý stav“ zařízení v danou hodinu
-- a hlášení odchylek od něj. Jeden řádek na zařízení a hodinu; last_at = stáří posledního započteného vzorku.
CREATE TABLE device_hourly (
    tenant_id uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    device_id uuid NOT NULL,
    hour      timestamptz NOT NULL,
    cpu_sum   integer NOT NULL DEFAULT 0,
    cpu_max   smallint NOT NULL DEFAULT 0,
    mem_sum   integer NOT NULL DEFAULT 0,
    samples   integer NOT NULL DEFAULT 0 CHECK (samples >= 0),
    last_at   timestamptz NOT NULL,
    PRIMARY KEY (device_id, hour),
    FOREIGN KEY (device_id, tenant_id) REFERENCES device(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX device_hourly_hour ON device_hourly (hour);

ALTER TABLE device_hourly ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_hourly FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON device_hourly USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());

GRANT SELECT, INSERT, UPDATE, DELETE ON device_hourly TO {app_role};
