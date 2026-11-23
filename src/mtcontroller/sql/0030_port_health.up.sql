-- Hodinové přírůstky chybových čítačů ethernetových portů podle druhu (FCS, zarovnání, fragmenty, kolize…)
-- a počty rámců, aby šla chybovost hodnotit poměrem k provozu. Plní se při uložení inventury rozdílem proti
-- předchozímu stavu čítačů. K tomu poslední optický výkon SFP v dané hodině (podklad pro pokles proti normálu).
CREATE TABLE port_error_hourly (
    tenant_id      uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    device_id      uuid NOT NULL,
    name           varchar(100) NOT NULL,
    hour           timestamptz NOT NULL,
    rx_packets     bigint NOT NULL DEFAULT 0,
    tx_packets     bigint NOT NULL DEFAULT 0,
    generic        bigint NOT NULL DEFAULT 0,   -- rx-error + tx-error z /interface (když čip podrobnosti nedává)
    fcs            bigint NOT NULL DEFAULT 0,
    align          bigint NOT NULL DEFAULT 0,
    fragment       bigint NOT NULL DEFAULT 0,
    jabber         bigint NOT NULL DEFAULT 0,
    size           bigint NOT NULL DEFAULT 0,
    overflow       bigint NOT NULL DEFAULT 0,
    late_collision bigint NOT NULL DEFAULT 0,
    other          bigint NOT NULL DEFAULT 0,
    collision      bigint NOT NULL DEFAULT 0,   -- běžné kolize (v half-duplexu nejsou chyba)
    drops          bigint NOT NULL DEFAULT 0,
    pause          bigint NOT NULL DEFAULT 0,
    sfp_rx         real,
    sfp_tx         real,
    PRIMARY KEY (device_id, name, hour),
    FOREIGN KEY (device_id, tenant_id) REFERENCES device(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX port_error_hourly_hour ON port_error_hourly (hour);

ALTER TABLE port_error_hourly ENABLE ROW LEVEL SECURITY;
ALTER TABLE port_error_hourly FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON port_error_hourly USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());

GRANT SELECT, INSERT, UPDATE, DELETE ON port_error_hourly TO {app_role};
