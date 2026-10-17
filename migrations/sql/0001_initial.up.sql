-- 0001: základní schéma F0 (tenanti, lokality, zařízení, uživatelé, audit).
-- {app_role} = role, pod kterou běží aplikace (bez BYPASSRLS).
--
-- RLS: každá tabulka s tenant_id má politiku
--   tenant_id = app.tenant_id  NEBO  app.operator = 'on'.
-- Obojí nastavuje aplikace per transakce (SET LOCAL). Bez kontextu se
-- nevrátí nic. RLS chrání před chybou v aplikační logice (zapomenutý
-- filtr); není náhradou za ochranu proti SQL injection.

CREATE OR REPLACE FUNCTION mtc_current_tenant() RETURNS uuid
LANGUAGE sql STABLE AS $$
  SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid
$$;

CREATE OR REPLACE FUNCTION mtc_is_operator() RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT coalesce(current_setting('app.operator', true), '') = 'on'
$$;

CREATE TYPE device_state AS ENUM ('pending', 'adopted', 'offline', 'removed');
CREATE TYPE device_role  AS ENUM ('unknown', 'gateway', 'switch', 'ap', 'ptp');
CREATE TYPE user_role    AS ENUM ('superadmin', 'tenant_admin', 'technician', 'readonly');
CREATE TYPE secret_kind  AS ENUM ('sstp_password', 'api_password', 'checkin_key', 'client_cert_key');

CREATE TABLE tenant (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name         varchar(200) NOT NULL UNIQUE,
    tunnel_block cidr NOT NULL UNIQUE,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE site (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    name               varchar(200) NOT NULL,
    address            text,
    maintenance_window varchar(100),
    created_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, name),
    UNIQUE (id, tenant_id)
);

CREATE TABLE device (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    site_id         uuid,
    serial          varchar(64) NOT NULL UNIQUE,
    identity        varchar(200),
    model           varchar(100),
    ros_version     varchar(50),
    role            device_role NOT NULL DEFAULT 'unknown',
    state           device_state NOT NULL DEFAULT 'pending',
    tunnel_ip       inet UNIQUE,
    last_checkin_at timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, tenant_id),
    -- lokalita musí patřit stejnému tenantovi jako zařízení
    FOREIGN KEY (site_id, tenant_id) REFERENCES site(id, tenant_id) ON DELETE SET NULL (site_id),
    CHECK (state <> 'adopted' OR (tunnel_ip IS NOT NULL AND site_id IS NOT NULL))
);

CREATE TABLE app_user (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email         varchar(320) NOT NULL UNIQUE CHECK (email = lower(email)),
    password_hash varchar(200) NOT NULL,
    totp_secret   bytea,
    is_active     boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE role_binding (
    id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id   uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    role      user_role NOT NULL,
    tenant_id uuid REFERENCES tenant(id) ON DELETE CASCADE,
    site_id   uuid REFERENCES site(id) ON DELETE CASCADE,
    UNIQUE NULLS NOT DISTINCT (user_id, tenant_id, site_id, role),
    -- superadmin je vždy globální, ostatní role vždy pod tenantem
    CHECK ((role = 'superadmin') = (tenant_id IS NULL)),
    CHECK (site_id IS NULL OR tenant_id IS NOT NULL)
);

CREATE TABLE device_secret (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    device_id   uuid NOT NULL,
    kind        secret_kind NOT NULL,
    wrapped_key bytea NOT NULL,
    ciphertext  bytea NOT NULL,
    key_version integer NOT NULL DEFAULT 1,
    rotated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (device_id, kind),
    FOREIGN KEY (device_id, tenant_id) REFERENCES device(id, tenant_id) ON DELETE CASCADE
);

CREATE TABLE adoption_token (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      uuid NOT NULL REFERENCES tenant(id) ON DELETE CASCADE,
    site_id        uuid,
    token_hash     char(64) NOT NULL UNIQUE,
    created_by     uuid NOT NULL REFERENCES app_user(id),
    expires_at     timestamptz NOT NULL,
    used_at        timestamptz,
    used_by_device uuid REFERENCES device(id),
    created_at     timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (site_id, tenant_id) REFERENCES site(id, tenant_id) ON DELETE CASCADE,
    CHECK (expires_at > created_at),
    CHECK ((used_at IS NULL) = (used_by_device IS NULL))
);

CREATE TABLE audit_log (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at        timestamptz NOT NULL DEFAULT now(),
    user_id   uuid REFERENCES app_user(id),
    tenant_id uuid REFERENCES tenant(id),
    action    varchar(100) NOT NULL,
    target    varchar(300),
    before    jsonb,
    after     jsonb,
    source_ip inet
);
CREATE INDEX audit_log_tenant_at ON audit_log (tenant_id, at DESC);

-- audit je append-only i pro vlastníka schématu
CREATE OR REPLACE FUNCTION mtc_audit_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'audit_log je append-only (% zakázán)', TG_OP;
END
$$;
CREATE TRIGGER audit_log_no_update BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION mtc_audit_immutable();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit_log
  FOR EACH STATEMENT EXECUTE FUNCTION mtc_audit_immutable();

-- adopční token jde použít jen jednou
CREATE OR REPLACE FUNCTION mtc_token_single_use() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.used_at IS NOT NULL THEN
    RAISE EXCEPTION 'adopční token už byl použit';
  END IF;
  IF NEW.token_hash <> OLD.token_hash OR NEW.tenant_id <> OLD.tenant_id
     OR NEW.expires_at <> OLD.expires_at THEN
    RAISE EXCEPTION 'adopční token nelze měnit';
  END IF;
  RETURN NEW;
END
$$;
CREATE TRIGGER adoption_token_single_use BEFORE UPDATE ON adoption_token
  FOR EACH ROW EXECUTE FUNCTION mtc_token_single_use();

-- row-level security -------------------------------------------------------

ALTER TABLE tenant ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON tenant
  USING (mtc_is_operator() OR id = mtc_current_tenant())
  WITH CHECK (mtc_is_operator());

ALTER TABLE site ENABLE ROW LEVEL SECURITY;
ALTER TABLE site FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON site
  USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());

ALTER TABLE device ENABLE ROW LEVEL SECURITY;
ALTER TABLE device FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON device
  USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());

ALTER TABLE device_secret ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_secret FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON device_secret
  USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());

ALTER TABLE adoption_token ENABLE ROW LEVEL SECURITY;
ALTER TABLE adoption_token FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON adoption_token
  USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());

ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log FORCE ROW LEVEL SECURITY;
CREATE POLICY audit_read ON audit_log FOR SELECT
  USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());
CREATE POLICY audit_write ON audit_log FOR INSERT
  WITH CHECK (mtc_is_operator() OR tenant_id = mtc_current_tenant());

-- práva aplikační role -----------------------------------------------------

GRANT USAGE ON SCHEMA public TO {app_role};
GRANT SELECT, INSERT, UPDATE, DELETE ON tenant, site, device, device_secret,
      adoption_token, app_user, role_binding TO {app_role};
GRANT SELECT, INSERT ON audit_log TO {app_role};
