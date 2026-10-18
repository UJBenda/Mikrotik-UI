-- 0014: řízený režim F5b – Wi-Fi sítě (SSID) lokality a role řízeného zařízení.
-- Heslo SSID je jen šifrované (SecretBox, kontext "ssid:<id>"), nikdy v plaintextu.

CREATE TABLE site_ssid (
    id              uuid PRIMARY KEY,
    tenant_id       uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    site_id         uuid NOT NULL,
    ssid            varchar(32) NOT NULL CHECK (length(ssid) BETWEEN 1 AND 32),
    vlan_id         integer NOT NULL CHECK (vlan_id BETWEEN 1 AND 4094),
    bands           varchar(8) NOT NULL DEFAULT '2.4,5' CHECK (bands IN ('2.4', '5', '2.4,5')),
    isolation       boolean NOT NULL DEFAULT false,
    ft              boolean NOT NULL DEFAULT true,
    enabled         boolean NOT NULL DEFAULT true,
    psk_wrapped_key bytea NOT NULL,
    psk_ciphertext  bytea NOT NULL,
    psk_key_version integer NOT NULL DEFAULT 1,
    updated_by      uuid REFERENCES app_user(id),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (site_id, ssid),
    FOREIGN KEY (site_id, tenant_id) REFERENCES site(id, tenant_id) ON DELETE CASCADE
);

ALTER TABLE site_ssid ENABLE ROW LEVEL SECURITY;
ALTER TABLE site_ssid FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON site_ssid USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());

GRANT SELECT, INSERT, UPDATE, DELETE ON site_ssid TO {app_role};

-- role řízeného zařízení: switch, nebo AP + switch (rádia řídí CAPsMAN lokality)
ALTER TABLE device_intent ADD COLUMN role varchar(10) CHECK (role IN ('switch', 'ap'));
