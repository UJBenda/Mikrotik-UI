-- Izolace řízených zařízení (lokální režim): bez výjimky smí zařízení samo jen na controller
-- a do privátních sítí. Výjimka „Povolit přístup do internetu“ je v záměru zařízení.
ALTER TABLE device_intent ADD COLUMN internet boolean NOT NULL DEFAULT false;
