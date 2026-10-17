-- 0010: profily portů (jako Port Profiles v UniFi) – pojmenovaná sada native VLAN,
-- tagovaných VLAN, PoE a STP edge, kterou lze použít na port. Hodnota NULL = profil
-- danou vlastnost nemění.

CREATE TABLE port_profile (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id  uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    name       varchar(64) NOT NULL,
    pvid       integer CHECK (pvid BETWEEN 1 AND 4094),
    tagged     varchar(400) CHECK (tagged ~ '^([0-9]{1,4}(,[0-9]{1,4})*)?$'),
    poe        varchar(16) CHECK (poe IN ('auto-on', 'off', 'forced-on')),
    edge       varchar(16) CHECK (edge IN ('auto', 'yes', 'no')),
    created_by uuid REFERENCES app_user(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name)
);

ALTER TABLE port_profile ENABLE ROW LEVEL SECURITY;
ALTER TABLE port_profile FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON port_profile USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());

GRANT SELECT, INSERT, DELETE ON port_profile TO {app_role};
