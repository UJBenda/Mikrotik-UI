-- Profil portu „všechny VLAN lokality“: tagged = '*' se při použití rozvine na VLAN sítí
-- lokality zařízení. Vestavěný profil (builtin) založí controller sám a nejde smazat.
ALTER TABLE port_profile DROP CONSTRAINT port_profile_tagged_check;
ALTER TABLE port_profile ADD CONSTRAINT port_profile_tagged_check
    CHECK (tagged ~ '^(\*|([0-9]{1,4}(,[0-9]{1,4})*)?)$');
ALTER TABLE port_profile ADD COLUMN builtin boolean NOT NULL DEFAULT false;
