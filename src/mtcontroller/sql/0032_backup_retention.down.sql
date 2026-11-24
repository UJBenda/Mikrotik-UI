DELETE FROM backup_target WHERE kind = 'dropbox';
ALTER TABLE backup_target DROP CONSTRAINT backup_target_kind_check;
ALTER TABLE backup_target ADD CONSTRAINT backup_target_kind_check CHECK (kind IN ('nextcloud', 'gdrive', 'onedrive'));
ALTER TABLE backup_target DROP COLUMN keep_weekly, DROP COLUMN keep_monthly;
