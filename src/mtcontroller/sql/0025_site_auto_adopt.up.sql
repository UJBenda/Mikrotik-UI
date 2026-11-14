-- Automatická adopce: zařízení ohlášené do lokality (token / klíč lokality) se schválí samo s rolí
-- podle modelu. Výchozí vypnuto – zapíná správce u lokality. Sloupec tabulky site (RLS beze změny).
ALTER TABLE site ADD COLUMN auto_adopt boolean NOT NULL DEFAULT false;
