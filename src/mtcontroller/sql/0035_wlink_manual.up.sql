-- Úseky přes nespravované zařízení se už nehledají samy (dělalo to desítky planých dvojic a falešné výpadky).
-- Hlídaný úsek zakládá správce ručně; automaticky nalezené se mažou i s měřeními.
DELETE FROM wlink WHERE kind = 'foreign';
ALTER TABLE wlink ADD COLUMN manual boolean NOT NULL DEFAULT false;
