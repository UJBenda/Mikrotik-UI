ALTER TABLE site DROP COLUMN unmanaged_scanned_at;
DROP TABLE unmanaged_port;
DELETE FROM wlink WHERE kind = 'foreign';
