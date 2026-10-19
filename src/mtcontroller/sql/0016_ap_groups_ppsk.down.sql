ALTER TABLE device_intent DROP COLUMN ap_group;
ALTER TABLE site_ssid DROP CONSTRAINT site_ssid_ppsk_both, DROP COLUMN ppsk_meta, DROP COLUMN ppsk_key_version,
  DROP COLUMN ppsk_ciphertext, DROP COLUMN ppsk_wrapped_key, DROP COLUMN groups;
