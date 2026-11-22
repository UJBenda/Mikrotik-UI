-- hodnotu 'wan_password' v enumu secret_kind PostgreSQL odebrat neumí; tajemství smažeme
DELETE FROM device_secret WHERE kind::text = 'wan_password';
DELETE FROM site_policy WHERE module = 'gateway';
