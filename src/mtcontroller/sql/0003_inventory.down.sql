ALTER TABLE device_command DROP COLUMN plan_id;
DROP TABLE IF EXISTS config_plan, site_policy, device_module, device_backup,
  interface_hourly, interface_sample, device_inventory;
DROP TYPE IF EXISTS plan_status, module_mode;
ALTER TABLE device DROP COLUMN inventory_at, DROP COLUMN inventory_error, DROP COLUMN backup_at;
