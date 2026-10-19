-- 0018: logy ze zařízení (syslog přes tunel) a test rychlosti mezi zařízeními (bandwidth-test).

-- úroveň logů, kterou má zařízení posílat: off | errors | info
ALTER TABLE device ADD COLUMN syslog_level varchar(10) NOT NULL DEFAULT 'off'
  CHECK (syslog_level IN ('off', 'errors', 'info'));

CREATE TABLE device_log (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id  uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    device_id  uuid NOT NULL,
    at         timestamptz NOT NULL DEFAULT now(),
    severity   smallint NOT NULL CHECK (severity BETWEEN 0 AND 7),
    topics     varchar(100) NOT NULL DEFAULT '',
    message    varchar(1000) NOT NULL,
    FOREIGN KEY (device_id, tenant_id) REFERENCES device(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX device_log_dev_at ON device_log (device_id, at DESC);
CREATE INDEX device_log_at ON device_log (at);

-- objem logů po dnech (kolik to „sežere“ na datech)
CREATE TABLE device_log_day (
    tenant_id  uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    device_id  uuid NOT NULL,
    day        date NOT NULL,
    lines      integer NOT NULL DEFAULT 0,
    bytes      bigint NOT NULL DEFAULT 0,
    dropped    integer NOT NULL DEFAULT 0,
    PRIMARY KEY (device_id, day),
    FOREIGN KEY (device_id, tenant_id) REFERENCES device(id, tenant_id) ON DELETE CASCADE
);

CREATE TABLE bw_test (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id   uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    from_device uuid NOT NULL,
    to_device   uuid NOT NULL,
    address     varchar(45) NOT NULL,
    params      jsonb NOT NULL DEFAULT '{}',
    status      varchar(10) NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'failed')),
    result      jsonb,
    error       varchar(500),
    created_by  uuid REFERENCES app_user(id),
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    FOREIGN KEY (from_device, tenant_id) REFERENCES device(id, tenant_id) ON DELETE CASCADE,
    FOREIGN KEY (to_device, tenant_id) REFERENCES device(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX bw_test_from ON bw_test (from_device, id DESC);

ALTER TABLE device_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_log FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON device_log USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());
ALTER TABLE device_log_day ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_log_day FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON device_log_day USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());
ALTER TABLE bw_test ENABLE ROW LEVEL SECURITY;
ALTER TABLE bw_test FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON bw_test USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());

GRANT SELECT, INSERT, UPDATE, DELETE ON device_log, device_log_day, bw_test TO {app_role};
