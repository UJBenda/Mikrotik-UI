-- 0003: inventura (jen čtení), metriky rozhraní, zálohy, režimy modulů,
-- politiky lokality a plány změn.

-- poslední stav jednotlivých částí konfigurace zařízení (REST, jen čtení)
CREATE TABLE device_inventory (
    tenant_id    uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    device_id    uuid NOT NULL,
    section      varchar(50) NOT NULL CHECK (section ~ '^[a-z_]+$'),
    data         jsonb NOT NULL,
    collected_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, section),
    FOREIGN KEY (device_id, tenant_id) REFERENCES device(id, tenant_id) ON DELETE CASCADE
);

ALTER TABLE device
  ADD COLUMN inventory_at    timestamptz,
  ADD COLUMN inventory_error text,
  ADD COLUMN backup_at       timestamptz,
  ADD COLUMN update_checked_at timestamptz;

-- surové vzorky čítačů rozhraní (retence krátká, viz hourly)
CREATE TABLE interface_sample (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    device_id uuid NOT NULL,
    at        timestamptz NOT NULL DEFAULT now(),
    name      varchar(100) NOT NULL,
    running   boolean,
    rx_bytes  bigint, tx_bytes bigint,
    rx_errors bigint, tx_errors bigint,
    rx_drops  bigint, tx_drops bigint,
    link_downs bigint,
    FOREIGN KEY (device_id, tenant_id) REFERENCES device(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX interface_sample_dev ON interface_sample (device_id, name, at DESC);
CREATE INDEX interface_sample_at ON interface_sample (at);

-- hodinové agregace (dlouhá retence): průměrná/špičková rychlost a přírůstky chyb
CREATE TABLE interface_hourly (
    tenant_id  uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    device_id  uuid NOT NULL,
    hour       timestamptz NOT NULL,
    name       varchar(100) NOT NULL,
    rx_bps_avg bigint, tx_bps_avg bigint,
    rx_bps_max bigint, tx_bps_max bigint,
    rx_errors  bigint, tx_errors bigint,
    rx_drops   bigint, tx_drops bigint,
    link_downs bigint,
    samples    integer NOT NULL,
    PRIMARY KEY (device_id, name, hour),
    FOREIGN KEY (device_id, tenant_id) REFERENCES device(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX interface_hourly_hour ON interface_hourly (hour);

-- zálohy konfigurace (export se show-sensitive) – vždy šifrovaně
CREATE TABLE device_backup (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id   uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    device_id   uuid NOT NULL,
    taken_at    timestamptz NOT NULL DEFAULT now(),
    reason      varchar(30) NOT NULL DEFAULT 'scheduled',
    size_bytes  integer NOT NULL,
    sha256      char(64) NOT NULL,
    -- sha256 exportu bez hlavičky s datem (pro detekci změn)
    content_sha char(64) NOT NULL,
    changed     boolean NOT NULL,
    wrapped_key bytea NOT NULL,
    ciphertext  bytea NOT NULL,
    key_version integer NOT NULL DEFAULT 1,
    FOREIGN KEY (device_id, tenant_id) REFERENCES device(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX device_backup_dev ON device_backup (device_id, taken_at DESC);

-- režim modulu na zařízení: observe (jen čte, výchozí) / manage (smí zapisovat)
CREATE TYPE module_mode AS ENUM ('observe', 'manage');
CREATE TABLE device_module (
    tenant_id  uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    device_id  uuid NOT NULL,
    module     varchar(30) NOT NULL CHECK (module ~ '^[a-z_]+$'),
    mode       module_mode NOT NULL DEFAULT 'observe',
    changed_by uuid REFERENCES app_user(id),
    changed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (device_id, module),
    FOREIGN KEY (device_id, tenant_id) REFERENCES device(id, tenant_id) ON DELETE CASCADE
);

-- požadovaný stav modulu pro lokalitu (např. NTP: primární zařízení, upstream servery)
CREATE TABLE site_policy (
    tenant_id  uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    site_id    uuid NOT NULL,
    module     varchar(30) NOT NULL CHECK (module ~ '^[a-z_]+$'),
    data       jsonb NOT NULL,
    updated_by uuid REFERENCES app_user(id),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (site_id, module),
    FOREIGN KEY (site_id, tenant_id) REFERENCES site(id, tenant_id) ON DELETE CASCADE
);

-- plán změn: vygenerovaný skript + lidsky čitelný popis; aplikuje se až po schválení
CREATE TYPE plan_status AS ENUM ('draft', 'queued', 'applied', 'failed', 'discarded', 'superseded');
CREATE TABLE config_plan (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id   uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    device_id   uuid NOT NULL,
    module      varchar(30) NOT NULL CHECK (module ~ '^[a-z_]+$'),
    summary     text NOT NULL,
    changes     jsonb NOT NULL,
    script      text NOT NULL,
    -- hash inventury, ze které plán vznikl – změní-li se zařízení, plán propadne
    basis_sha   char(64) NOT NULL,
    status      plan_status NOT NULL DEFAULT 'draft',
    created_by  uuid REFERENCES app_user(id),
    created_at  timestamptz NOT NULL DEFAULT now(),
    approved_by uuid REFERENCES app_user(id),
    approved_at timestamptz,
    command_id  bigint REFERENCES device_command(id),
    finished_at timestamptz,
    FOREIGN KEY (device_id, tenant_id) REFERENCES device(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX config_plan_dev ON config_plan (device_id, status);

ALTER TABLE device_command ADD COLUMN plan_id bigint REFERENCES config_plan(id);

-- RLS pro všechny nové tabulky
ALTER TABLE device_inventory ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_inventory FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON device_inventory USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());
ALTER TABLE interface_sample ENABLE ROW LEVEL SECURITY;
ALTER TABLE interface_sample FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON interface_sample USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());
ALTER TABLE interface_hourly ENABLE ROW LEVEL SECURITY;
ALTER TABLE interface_hourly FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON interface_hourly USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());
ALTER TABLE device_backup ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_backup FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON device_backup USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());
ALTER TABLE device_module ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_module FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON device_module USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());
ALTER TABLE site_policy ENABLE ROW LEVEL SECURITY;
ALTER TABLE site_policy FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON site_policy USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());
ALTER TABLE config_plan ENABLE ROW LEVEL SECURITY;
ALTER TABLE config_plan FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON config_plan USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());

GRANT SELECT, INSERT, UPDATE, DELETE ON device_inventory, interface_sample, interface_hourly,
      device_module, site_policy TO {app_role};
GRANT SELECT, INSERT, DELETE ON device_backup TO {app_role};
GRANT SELECT, INSERT, UPDATE ON config_plan TO {app_role};
