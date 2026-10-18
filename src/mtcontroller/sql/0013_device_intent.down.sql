-- hodnotu 'takeover_backup' v enumu secret_kind PostgreSQL odebrat neumí; tajemství smažeme
DELETE FROM device_secret WHERE kind::text = 'takeover_backup';
ALTER TABLE device DROP COLUMN managed_at;
DROP TABLE device_intent;
