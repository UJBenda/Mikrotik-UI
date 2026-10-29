DELETE FROM port_profile WHERE tagged = '*';
ALTER TABLE port_profile DROP COLUMN builtin;
ALTER TABLE port_profile DROP CONSTRAINT port_profile_tagged_check;
ALTER TABLE port_profile ADD CONSTRAINT port_profile_tagged_check
    CHECK (tagged ~ '^([0-9]{1,4}(,[0-9]{1,4})*)?$');
