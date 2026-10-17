-- 0002: check-in, fronta příkazů, přihlašovací relace, MFA.

ALTER TYPE device_state ADD VALUE IF NOT EXISTS 'provisioning' BEFORE 'adopted';

ALTER TABLE device
  ADD COLUMN checkin_key_hash     char(64) UNIQUE,
  ADD COLUMN mac                  varchar(17),
  -- SHA-256 otisk certifikátu www-ssl zařízení (pinning REST přes tunel)
  ADD COLUMN api_cert_fingerprint char(64),
  ADD COLUMN last_status          jsonb,
  ADD COLUMN approved_by          uuid REFERENCES app_user(id),
  ADD COLUMN approved_at          timestamptz,
  ADD COLUMN adopted_at           timestamptz,
  ADD COLUMN concentrator         varchar(50);

ALTER TABLE app_user
  ADD COLUMN totp_wrapped_key bytea,
  ADD COLUMN totp_confirmed   boolean NOT NULL DEFAULT false,
  ADD COLUMN totp_last_step   bigint,
  ADD COLUMN failed_logins    integer NOT NULL DEFAULT 0,
  ADD COLUMN locked_until     timestamptz;
ALTER TABLE app_user RENAME COLUMN totp_secret TO totp_ciphertext;

-- přihlašovací relace (globální, bez RLS – uživatel může mít víc tenantů)
CREATE TABLE app_session (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash   char(64) NOT NULL UNIQUE,
    user_id      uuid NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
    mfa_ok       boolean NOT NULL DEFAULT false,
    csrf_token   char(64) NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    source_ip    inet
);
CREATE INDEX app_session_user ON app_session (user_id);

-- fronta příkazů pro zařízení. Neobsahuje skript ani tajemství – skript se
-- vyrenderuje až při doručení z `kind` + `params` a šifrovaných tajemství.
CREATE TYPE command_status AS ENUM ('queued', 'sent', 'done', 'failed', 'cancelled');

CREATE TABLE device_command (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id   uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    device_id   uuid NOT NULL,
    kind        varchar(50) NOT NULL,
    params      jsonb NOT NULL DEFAULT '{}',
    status      command_status NOT NULL DEFAULT 'queued',
    attempts    integer NOT NULL DEFAULT 0,
    created_by  uuid REFERENCES app_user(id),
    created_at  timestamptz NOT NULL DEFAULT now(),
    sent_at     timestamptz,
    finished_at timestamptz,
    result      text,
    FOREIGN KEY (device_id, tenant_id) REFERENCES device(id, tenant_id) ON DELETE CASCADE,
    CHECK (kind ~ '^[a-z_]+$'),
    CHECK (attempts BETWEEN 0 AND 10)
);
CREATE INDEX device_command_pending ON device_command (device_id, id) WHERE status IN ('queued', 'sent');

ALTER TABLE device_command ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_command FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON device_command
  USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());

GRANT SELECT, INSERT, UPDATE, DELETE ON app_session TO {app_role};
GRANT SELECT, INSERT, UPDATE ON device_command TO {app_role};
