ALTER TABLE config_plan DROP COLUMN IF EXISTS revert_script, DROP COLUMN IF EXISTS confirm_minutes,
  DROP COLUMN IF EXISTS confirm_deadline, DROP COLUMN IF EXISTS confirmed_at;
-- hodnoty enumu nejde odebrat přímo: převést stavy a typ vytvořit znovu
UPDATE config_plan SET status = 'applied' WHERE status::text = 'confirming';
UPDATE config_plan SET status = 'failed' WHERE status::text = 'rolled_back';
ALTER TABLE config_plan ALTER COLUMN status DROP DEFAULT;
ALTER TYPE plan_status RENAME TO plan_status_old;
CREATE TYPE plan_status AS ENUM ('draft', 'queued', 'applied', 'failed', 'discarded', 'superseded');
ALTER TABLE config_plan ALTER COLUMN status TYPE plan_status USING status::text::plan_status;
ALTER TABLE config_plan ALTER COLUMN status SET DEFAULT 'draft';
DROP TYPE plan_status_old;
