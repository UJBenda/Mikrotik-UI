-- 0006: poznámka k zařízení (umístění, kontakt, …) – zadává správce v UI.
ALTER TABLE device ADD COLUMN note varchar(500) NOT NULL DEFAULT '';
