-- Příprava zařízení: kdy si skript někdo stáhl (soubor) nebo ho vyzvedlo zařízení (jednořádkový příkaz).
ALTER TABLE device_prep ADD COLUMN fetched_at timestamptz;
