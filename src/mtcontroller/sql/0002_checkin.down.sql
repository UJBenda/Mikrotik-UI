DROP TABLE IF EXISTS device_command, app_session;
DROP TYPE IF EXISTS command_status;
ALTER TABLE app_user RENAME COLUMN totp_ciphertext TO totp_secret;
ALTER TABLE app_user DROP COLUMN totp_wrapped_key, DROP COLUMN totp_confirmed, DROP COLUMN totp_last_step,
  DROP COLUMN failed_logins, DROP COLUMN locked_until;
ALTER TABLE device DROP COLUMN checkin_key_hash, DROP COLUMN mac,
  DROP COLUMN api_cert_fingerprint, DROP COLUMN last_status, DROP COLUMN approved_by,
  DROP COLUMN approved_at, DROP COLUMN adopted_at, DROP COLUMN concentrator;
-- hodnotu 'provisioning' z enumu device_state nelze v PostgreSQL odebrat; zůstává.
