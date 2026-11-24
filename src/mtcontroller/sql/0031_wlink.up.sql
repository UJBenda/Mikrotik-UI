-- Bezdrátové spoje mezi dvěma spravovanými zařízeními (PtP/PtMP) a jejich měření po ~5 minutách:
-- signál obou stran, rychlost spoje, CCQ, šum a aktivní měření ztrátovosti a odezvy pingem přes spoj.
CREATE TABLE wlink (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    site_id    uuid NOT NULL,
    kind       varchar(16) NOT NULL,            -- wifi | wireless (ovladač strany A)
    a_device   uuid NOT NULL,                   -- strana A = AP/bridge
    a_iface    varchar(100) NOT NULL,
    a_mac      varchar(17) NOT NULL,
    b_device   uuid NOT NULL,                   -- strana B = klient (station)
    b_iface    varchar(100) NOT NULL,
    b_mac      varchar(17) NOT NULL,
    ping_ip    inet,                            -- adresa strany B, na kterou A měří ztrátovost
    muted      boolean NOT NULL DEFAULT false,  -- nehlásit alerty (známý slabý spoj)
    first_seen timestamptz NOT NULL DEFAULT now(),
    last_seen  timestamptz NOT NULL DEFAULT now(),
    sampled_at timestamptz,
    UNIQUE (id, tenant_id),
    UNIQUE (a_device, a_iface, b_device, b_iface),
    FOREIGN KEY (site_id, tenant_id) REFERENCES site(id, tenant_id) ON DELETE CASCADE,
    FOREIGN KEY (a_device, tenant_id) REFERENCES device(id, tenant_id) ON DELETE CASCADE,
    FOREIGN KEY (b_device, tenant_id) REFERENCES device(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX wlink_site ON wlink (site_id);

CREATE TABLE wlink_sample (
    tenant_id uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    link_id   uuid NOT NULL,
    at        timestamptz NOT NULL DEFAULT now(),
    up        boolean NOT NULL,
    signal_a  smallint,      -- dBm, jak slyší A stranu B
    signal_b  smallint,
    snr_a     smallint,
    snr_b     smallint,
    rate_ab   bigint,        -- bit/s, vyjednaná rychlost A → B
    rate_ba   bigint,
    ccq_a     smallint,      -- % (jen starý ovladač wireless)
    ccq_b     smallint,
    noise     smallint,      -- dBm, šum na kanálu strany A (pokud ho ovladač dává)
    loss      real,          -- % ztracených pingů A → B
    rtt_avg   real,          -- ms
    rtt_min   real,
    rtt_max   real,
    uptime_s  integer,       -- doba spojení; pokles = přepojení
    reconnect boolean NOT NULL DEFAULT false,
    PRIMARY KEY (link_id, at),
    FOREIGN KEY (link_id, tenant_id) REFERENCES wlink(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX wlink_sample_at ON wlink_sample (at);

ALTER TABLE wlink ENABLE ROW LEVEL SECURITY;
ALTER TABLE wlink FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON wlink USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());
ALTER TABLE wlink_sample ENABLE ROW LEVEL SECURITY;
ALTER TABLE wlink_sample FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON wlink_sample USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());

GRANT SELECT, INSERT, UPDATE, DELETE ON wlink, wlink_sample TO {app_role};
