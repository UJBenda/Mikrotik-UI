-- 0008: historie klientů lokality (poprvé/naposledy viděn, poslední poloha).
-- Plní worker z korelace inventur (clients.correlate) každých 5 minut.

CREATE TABLE client (
    tenant_id  uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    site_id    uuid NOT NULL,
    mac        varchar(17) NOT NULL CHECK (mac ~ '^[0-9A-F]{2}(:[0-9A-F]{2}){5}$'),
    first_seen timestamptz NOT NULL DEFAULT now(),
    last_seen  timestamptz NOT NULL DEFAULT now(),
    ip         varchar(45),
    hostname   varchar(255),
    device_id  uuid,
    device     varchar(200),
    port       varchar(100),
    vlan       varchar(20),
    ssid       varchar(64),
    signal     varchar(20),
    PRIMARY KEY (site_id, mac),
    FOREIGN KEY (site_id, tenant_id) REFERENCES site(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX client_tenant_seen ON client (tenant_id, last_seen DESC);
CREATE INDEX client_tenant_ip ON client (tenant_id, ip);

ALTER TABLE client ENABLE ROW LEVEL SECURITY;
ALTER TABLE client FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON client USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());

GRANT SELECT, INSERT, UPDATE, DELETE ON client TO {app_role};
