-- 0019: Wi-Fi diagnostika klientů – události (připojení, odpojení s důvodem, roaming) z logů zařízení
-- a vzorky z registrační tabulky (AP, rozhraní, signál, rychlost) pro časovou osu klienta.

ALTER TABLE device ADD COLUMN syslog_wifi boolean NOT NULL DEFAULT false;

CREATE TABLE wifi_event (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id  uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    site_id    uuid,
    device_id  uuid NOT NULL,
    mac        varchar(17) NOT NULL CHECK (mac ~ '^[0-9A-F]{2}(:[0-9A-F]{2}){5}$'),
    at         timestamptz NOT NULL DEFAULT now(),
    kind       varchar(16) NOT NULL CHECK (kind IN ('connected', 'disconnected', 'roamed', 'reassoc', 'auth_fail', 'other')),
    iface      varchar(64) NOT NULL DEFAULT '',
    signal     smallint,
    ft         boolean NOT NULL DEFAULT false,
    reason     varchar(200) NOT NULL DEFAULT '',
    FOREIGN KEY (device_id, tenant_id) REFERENCES device(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX wifi_event_site_mac ON wifi_event (site_id, mac, at);
CREATE INDEX wifi_event_at ON wifi_event (at);

CREATE TABLE wifi_sample (
    tenant_id  uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    site_id    uuid,
    device_id  uuid NOT NULL,
    mac        varchar(17) NOT NULL CHECK (mac ~ '^[0-9A-F]{2}(:[0-9A-F]{2}){5}$'),
    at         timestamptz NOT NULL DEFAULT now(),
    iface      varchar(64) NOT NULL DEFAULT '',
    ssid       varchar(64) NOT NULL DEFAULT '',
    signal     smallint,
    tx_rate    varchar(40) NOT NULL DEFAULT '',
    rx_rate    varchar(40) NOT NULL DEFAULT '',
    uptime     varchar(40) NOT NULL DEFAULT '',
    FOREIGN KEY (device_id, tenant_id) REFERENCES device(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX wifi_sample_site_mac ON wifi_sample (site_id, mac, at);
CREATE INDEX wifi_sample_at ON wifi_sample (at);

ALTER TABLE wifi_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE wifi_event FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON wifi_event USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());
ALTER TABLE wifi_sample ENABLE ROW LEVEL SECURITY;
ALTER TABLE wifi_sample FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON wifi_sample USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());

GRANT SELECT, INSERT, UPDATE, DELETE ON wifi_event, wifi_sample TO {app_role};
