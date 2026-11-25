-- Cestování v čase na mapě: snímky topologie (ukládají se jen při změně) a přesuny klientů mezi porty / AP.
-- Z přesunů se navíc pozná přeskakování MAC mezi porty (smyčka, duplicitní MAC). Retence 30 dní.
CREATE TABLE infra_snapshot (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    site_id   uuid NOT NULL,
    at        timestamptz NOT NULL DEFAULT now(),
    sig       varchar(40) NOT NULL,
    data      jsonb NOT NULL,
    FOREIGN KEY (site_id, tenant_id) REFERENCES site(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX infra_snapshot_site ON infra_snapshot (site_id, at DESC);

CREATE TABLE client_move (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    site_id   uuid NOT NULL,
    mac       varchar(17) NOT NULL CHECK (mac ~ '^[0-9A-F]{2}(:[0-9A-F]{2}){5}$'),
    at        timestamptz NOT NULL DEFAULT now(),
    device_id uuid NOT NULL,
    port      varchar(100),
    ssid      varchar(64),
    FOREIGN KEY (site_id, tenant_id) REFERENCES site(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX client_move_mac ON client_move (site_id, mac, at DESC);
CREATE INDEX client_move_at ON client_move (at);

ALTER TABLE infra_snapshot ENABLE ROW LEVEL SECURITY;
ALTER TABLE infra_snapshot FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON infra_snapshot USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());
ALTER TABLE client_move ENABLE ROW LEVEL SECURITY;
ALTER TABLE client_move FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON client_move USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());

GRANT SELECT, INSERT, UPDATE, DELETE ON infra_snapshot, client_move TO {app_role};
