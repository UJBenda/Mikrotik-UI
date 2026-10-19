-- 0016: Wi-Fi – skupiny AP a více hesel na jedno SSID (multi-passphrase, každé heslo svou VLAN).
-- groups = id skupin AP ze záměru lokality, kde se SSID vysílá ([] = všechna AP).
-- Hesla multi-passphrase jen šifrovaně jako jeden blob (SecretBox, kontext "ssid:<id>:ppsk");
-- ppsk_meta = jména a VLAN hesel bez hesel (přehled, kontrola sítí v záměru bez dešifrování).

ALTER TABLE site_ssid
  ADD COLUMN groups           jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(groups) = 'array'),
  ADD COLUMN ppsk_wrapped_key bytea,
  ADD COLUMN ppsk_ciphertext  bytea,
  ADD COLUMN ppsk_key_version integer,
  ADD COLUMN ppsk_meta        jsonb NOT NULL DEFAULT '[]'
    CHECK (jsonb_typeof(ppsk_meta) = 'array' AND jsonb_array_length(ppsk_meta) <= 10000),
  ADD CONSTRAINT site_ssid_ppsk_both CHECK ((ppsk_wrapped_key IS NULL) = (ppsk_ciphertext IS NULL)
                                            AND (ppsk_wrapped_key IS NULL) = (jsonb_array_length(ppsk_meta) = 0));

-- skupina AP řízeného zařízení (id ze záměru lokality; NULL = výchozí „všechna AP“)
ALTER TABLE device_intent ADD COLUMN ap_group varchar(24) CHECK (ap_group ~ '^[a-z0-9-]{1,24}$');
