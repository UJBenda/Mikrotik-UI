-- Stupňovaná retence kopií záloh v cloudu (týdenní a měsíční zálohy navíc k posledním N) a nový cíl Dropbox.
ALTER TABLE backup_target
    ADD COLUMN keep_weekly  integer NOT NULL DEFAULT 0 CHECK (keep_weekly BETWEEN 0 AND 104),
    ADD COLUMN keep_monthly integer NOT NULL DEFAULT 0 CHECK (keep_monthly BETWEEN 0 AND 120);
ALTER TABLE backup_target DROP CONSTRAINT backup_target_kind_check;
ALTER TABLE backup_target ADD CONSTRAINT backup_target_kind_check
    CHECK (kind IN ('nextcloud', 'gdrive', 'onedrive', 'dropbox'));
