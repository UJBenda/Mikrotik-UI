-- Provoz podle klientů (NetFlow v9 / IPFIX z brány přes tunel; příjem ve službě mtc-syslog, flow.py).
-- Ukládají se jen hodinové součty: místní klient × protistrana × protokol × port služby.
-- Řádek s peer 0.0.0.0, proto 0, port 0 = „ostatní“ (součet toho, co se nevešlo do limitu řádků).
ALTER TABLE device ADD COLUMN flow_enabled boolean NOT NULL DEFAULT false;

CREATE TABLE flow_hourly (
    tenant_id  uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    site_id    uuid NOT NULL,
    device_id  uuid NOT NULL,
    hour       timestamptz NOT NULL,
    client     inet NOT NULL,
    peer       inet NOT NULL,
    proto      smallint NOT NULL,
    port       integer NOT NULL CHECK (port BETWEEN 0 AND 65535),
    bytes_up   bigint NOT NULL DEFAULT 0,
    bytes_down bigint NOT NULL DEFAULT 0,
    packets    bigint NOT NULL DEFAULT 0,
    flows      integer NOT NULL DEFAULT 0,
    PRIMARY KEY (device_id, hour, client, peer, proto, port),
    FOREIGN KEY (site_id, tenant_id) REFERENCES site(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX flow_hourly_site ON flow_hourly (site_id, hour);
CREATE INDEX flow_hourly_hour ON flow_hourly (hour);

ALTER TABLE flow_hourly ENABLE ROW LEVEL SECURITY;
ALTER TABLE flow_hourly FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON flow_hourly USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());

GRANT SELECT, INSERT, UPDATE, DELETE ON flow_hourly TO {app_role};
