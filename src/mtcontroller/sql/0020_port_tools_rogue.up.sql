-- 0020: PoE hlídač v profilu portu (úprava profilů) a Wi-Fi sítě viděné skenem AP
-- (detekce podvržených AP – cizí BSSID vysílá naše SSID – a sousedních sítí).

ALTER TABLE port_profile ADD COLUMN poe_watchdog boolean;
ALTER TABLE port_profile ADD COLUMN poe_watchdog_timeout integer CHECK (poe_watchdog_timeout BETWEEN 10 AND 3600);
GRANT UPDATE ON port_profile TO {app_role};

CREATE TABLE wifi_bss (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id   uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    site_id     uuid NOT NULL,
    bssid       varchar(17) NOT NULL CHECK (bssid ~ '^[0-9A-F]{2}(:[0-9A-F]{2}){5}$'),
    ssid        varchar(64) NOT NULL DEFAULT '',
    frequency   integer,
    signal      smallint,
    security    varchar(80) NOT NULL DEFAULT '',
    -- own = naše AP, rogue = cizí BSSID s naším SSID, neighbor = cizí síť
    kind        varchar(10) NOT NULL CHECK (kind IN ('own', 'rogue', 'neighbor')),
    seen_by     uuid,
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now(),
    -- správce může cizí BSSID označit jako známé (soused, vlastní zařízení mimo správu)
    acknowledged boolean NOT NULL DEFAULT false,
    UNIQUE (site_id, bssid),
    FOREIGN KEY (site_id, tenant_id) REFERENCES site(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX wifi_bss_site_kind ON wifi_bss (site_id, kind, last_seen);

ALTER TABLE wifi_bss ENABLE ROW LEVEL SECURITY;
ALTER TABLE wifi_bss FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON wifi_bss USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());

GRANT SELECT, INSERT, UPDATE, DELETE ON wifi_bss TO {app_role};
