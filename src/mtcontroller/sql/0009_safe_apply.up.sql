-- 0009: bezpečné aplikování změn (commit-confirm, obdoba UniFi SafeOps).
-- Plán s revert_script se na zařízení aplikuje s automatickým návratem: když ho
-- controller do confirm_minutes nepotvrdí (zařízení se neozve), zařízení spustí revert.

ALTER TYPE plan_status ADD VALUE IF NOT EXISTS 'confirming';
ALTER TYPE plan_status ADD VALUE IF NOT EXISTS 'rolled_back';

ALTER TABLE config_plan
  ADD COLUMN revert_script    text,
  ADD COLUMN confirm_minutes  integer CHECK (confirm_minutes BETWEEN 2 AND 30),
  ADD COLUMN confirm_deadline timestamptz,
  ADD COLUMN confirmed_at     timestamptz;
