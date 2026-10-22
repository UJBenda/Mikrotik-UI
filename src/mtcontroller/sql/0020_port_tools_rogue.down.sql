DROP TABLE wifi_bss;
REVOKE UPDATE ON port_profile FROM {app_role};
ALTER TABLE port_profile DROP COLUMN poe_watchdog_timeout;
ALTER TABLE port_profile DROP COLUMN poe_watchdog;
