-- LACP na řízených zařízeních: bondy jsou součást záměru zařízení (sladění je vytvoří i zruší).
-- [{name, ports: [port, …], mode: trunk|access|hybrid, vlan, tagged, lacp_rate, hash}]
ALTER TABLE device_intent ADD COLUMN bonds jsonb NOT NULL DEFAULT '[]';
