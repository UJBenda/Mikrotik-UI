-- Relace klientů: kdy byl klient připojený (pro „kolik klientů bylo v danou dobu“ a posun v čase).
-- Otevřená relace má ended_at NULL; seen_at = kdy ji worker naposledy potvrdil.
CREATE TABLE client_session (
    tenant_id  uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    site_id    uuid NOT NULL,
    mac        varchar(17) NOT NULL CHECK (mac ~ '^[0-9A-F]{2}(:[0-9A-F]{2}){5}$'),
    started_at timestamptz NOT NULL DEFAULT now(),
    seen_at    timestamptz NOT NULL DEFAULT now(),
    ended_at   timestamptz,
    wifi       boolean NOT NULL DEFAULT false,
    device     varchar(200),
    port       varchar(100),
    ssid       varchar(64),
    vlan       varchar(20),
    PRIMARY KEY (site_id, mac, started_at),
    FOREIGN KEY (site_id, tenant_id) REFERENCES site(id, tenant_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX client_session_open ON client_session (site_id, mac) WHERE ended_at IS NULL;
CREATE INDEX client_session_span ON client_session (site_id, started_at, ended_at);

ALTER TABLE client_session ENABLE ROW LEVEL SECURITY;
ALTER TABLE client_session FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON client_session USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());

GRANT SELECT, INSERT, UPDATE, DELETE ON client_session TO {app_role};
