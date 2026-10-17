-- 0005: changelogy verzí RouterOS (globální data, ne data tenanta).
-- Číst smí každý (release notes u aktualizace), zapisovat jen provozovatel (worker).

CREATE TABLE ros_release (
    version     varchar(40) PRIMARY KEY CHECK (version ~ '^[0-9]+\.[0-9]+(\.[0-9]+)?((beta|rc)[0-9]+)?$'),
    released_at varchar(40),
    changelog   text NOT NULL CHECK (length(changelog) <= 524288),
    fetched_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE ros_release ENABLE ROW LEVEL SECURITY;
ALTER TABLE ros_release FORCE ROW LEVEL SECURITY;
CREATE POLICY read_all ON ros_release FOR SELECT USING (true);
CREATE POLICY operator_insert ON ros_release FOR INSERT WITH CHECK (mtc_is_operator());
CREATE POLICY operator_update ON ros_release FOR UPDATE USING (mtc_is_operator()) WITH CHECK (mtc_is_operator());

GRANT SELECT, INSERT, UPDATE ON ros_release TO {app_role};
