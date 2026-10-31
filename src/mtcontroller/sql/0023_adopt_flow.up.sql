-- Adopce na jeden krok: stav automatického řetězce po schválení (provisioning → inventura →
-- balíček Wi-Fi → převzetí do řízení → Wi-Fi). Sloupec tabulky device – RLS platí jako dřív.
ALTER TABLE device ADD COLUMN adopt_flow jsonb;
CREATE INDEX device_adopt_flow_active ON device ((adopt_flow->>'step'))
    WHERE adopt_flow IS NOT NULL AND adopt_flow->>'step' NOT IN ('done', 'failed', 'needs_confirm');
