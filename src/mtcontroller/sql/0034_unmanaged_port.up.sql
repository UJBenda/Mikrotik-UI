-- Porty, za kterými je nespravované zařízení, které větví (hloupý switch, cizí spoj): evidence a schvalování.
-- Nově přibylý takový port hlásí alert unmanaged_switch; první průchod lokality jen zapíše výchozí stav.
CREATE TABLE unmanaged_port (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    site_id     uuid NOT NULL,
    device_id   uuid NOT NULL,
    port        varchar(100) NOT NULL,
    clients     integer NOT NULL DEFAULT 0,      -- drátových klientů za portem
    shared      boolean NOT NULL DEFAULT false,  -- za portem je i spravované zařízení
    baseline    boolean NOT NULL DEFAULT false,  -- nalezeno při prvním průchodu lokality (nehlásí se)
    approved    boolean NOT NULL DEFAULT false,
    approved_by uuid REFERENCES app_user(id) ON DELETE SET NULL,
    approved_at timestamptz,
    note        varchar(200),
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (device_id, port),
    FOREIGN KEY (site_id, tenant_id) REFERENCES site(id, tenant_id) ON DELETE CASCADE,
    FOREIGN KEY (device_id, tenant_id) REFERENCES device(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX unmanaged_port_site ON unmanaged_port (site_id);
ALTER TABLE unmanaged_port ENABLE ROW LEVEL SECURITY;
ALTER TABLE unmanaged_port FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON unmanaged_port USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());
GRANT SELECT, INSERT, UPDATE, DELETE ON unmanaged_port TO {app_role};

-- kdy lokalita poprvé prošla hledáním nespravovaných switchů (do té doby se zapisuje jen výchozí stav)
ALTER TABLE site ADD COLUMN unmanaged_scanned_at timestamptz;
