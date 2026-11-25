-- Odezva a ztrátovost internetu: brána lokality každých 5 minut pingne veřejné cíle (graf na Přehledu,
-- dostupnost linky). Retence 30 dní.
CREATE TABLE wan_probe (
    tenant_id uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    site_id   uuid NOT NULL,
    device_id uuid NOT NULL,
    at        timestamptz NOT NULL DEFAULT now(),
    target    varchar(45) NOT NULL,
    loss      real NOT NULL,
    rtt_avg   real,
    PRIMARY KEY (site_id, target, at),
    FOREIGN KEY (site_id, tenant_id) REFERENCES site(id, tenant_id) ON DELETE CASCADE,
    FOREIGN KEY (device_id, tenant_id) REFERENCES device(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX wan_probe_at ON wan_probe (at);

ALTER TABLE wan_probe ENABLE ROW LEVEL SECURITY;
ALTER TABLE wan_probe FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON wan_probe USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());

GRANT SELECT, INSERT, UPDATE, DELETE ON wan_probe TO {app_role};
