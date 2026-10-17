-- 0011: historie portů (jako Port Time Machine v UniFi) – změny stavu a nastavení
-- fyzických portů zjištěné porovnáním po sobě jdoucích inventur. Retence 90 dní.

CREATE TABLE port_event (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    device_id uuid NOT NULL,
    at        timestamptz NOT NULL DEFAULT now(),
    port      varchar(100) NOT NULL,
    field     varchar(32) NOT NULL,
    old       varchar(300),
    new       varchar(300),
    FOREIGN KEY (device_id, tenant_id) REFERENCES device(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX port_event_dev ON port_event (device_id, port, at DESC);
CREATE INDEX port_event_at ON port_event (at);

ALTER TABLE port_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE port_event FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON port_event USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());

GRANT SELECT, INSERT, DELETE ON port_event TO {app_role};
