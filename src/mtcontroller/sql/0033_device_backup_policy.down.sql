DROP TABLE device_backup_policy;
ALTER TABLE device DROP COLUMN backup_enabled, DROP COLUMN backup_error, DROP COLUMN backup_retry_at;
