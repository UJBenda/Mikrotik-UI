-- 0017: klienti – popisek, ikona a vlastní loga (výrobce nebo konkrétní klient).
-- Loga jsou obrázky nahrané uživatelem (PNG/JPEG/WebP, max. 64 KiB, kontrola hlavičky v aplikaci);
-- SVG záměrně ne (může obsahovat skript).

ALTER TABLE client_note
  ADD COLUMN label varchar(64) NOT NULL DEFAULT '',
  ADD COLUMN icon  varchar(24) NOT NULL DEFAULT '' CHECK (icon ~ '^[a-z]{0,24}$'),
  ALTER COLUMN note SET DEFAULT '';

CREATE TABLE client_logo (
    tenant_id  uuid NOT NULL REFERENCES tenant(id) ON DELETE RESTRICT,
    scope      varchar(10) NOT NULL CHECK (scope IN ('vendor', 'client')),
    key        varchar(120) NOT NULL CHECK (length(key) > 0),
    mime       varchar(20) NOT NULL CHECK (mime IN ('image/png', 'image/jpeg', 'image/webp')),
    data       bytea NOT NULL CHECK (octet_length(data) <= 65536),
    sha        char(16) NOT NULL,
    updated_by uuid REFERENCES app_user(id),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, scope, key)
);

ALTER TABLE client_logo ENABLE ROW LEVEL SECURITY;
ALTER TABLE client_logo FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON client_logo USING (mtc_is_operator() OR tenant_id = mtc_current_tenant());

GRANT SELECT, INSERT, UPDATE, DELETE ON client_logo TO {app_role};
