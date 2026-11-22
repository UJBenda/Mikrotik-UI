-- Řízená brána: heslo PPPoE (WAN) jako tajemství zařízení. Nastavení brány je v site_policy (modul gateway).
ALTER TYPE secret_kind ADD VALUE IF NOT EXISTS 'wan_password';
