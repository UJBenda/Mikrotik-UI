DROP TABLE IF EXISTS audit_log, adoption_token, device_secret, role_binding,
  app_user, device, site, tenant CASCADE;
DROP TYPE IF EXISTS secret_kind, user_role, device_role, device_state;
DROP FUNCTION IF EXISTS mtc_token_single_use(), mtc_audit_immutable(),
  mtc_is_operator(), mtc_current_tenant();
