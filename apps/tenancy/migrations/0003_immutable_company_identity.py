from django.db import migrations

def install(apps, schema_editor):
    if schema_editor.connection.vendor == "sqlite":
        schema_editor.execute("CREATE TRIGGER tenancy_uuid_immutable BEFORE UPDATE OF uuid ON tenancy_tenant WHEN OLD.uuid != NEW.uuid BEGIN SELECT RAISE(ABORT, 'Company UUID is immutable'); END")
        for operation in ("UPDATE", "DELETE"):
            schema_editor.execute(f"CREATE TRIGGER platform_audit_{operation.lower()} BEFORE {operation} ON tenancy_platformauditevent BEGIN SELECT RAISE(ABORT, 'Platform audit is append-only'); END")
    elif schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("CREATE FUNCTION helpdesk_immutable_company_uuid() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF OLD.uuid <> NEW.uuid THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Company UUID is immutable'; END IF; RETURN NEW; END $$")
        schema_editor.execute("CREATE TRIGGER tenancy_uuid_immutable BEFORE UPDATE OF uuid ON tenancy_tenant FOR EACH ROW EXECUTE FUNCTION helpdesk_immutable_company_uuid()")
        schema_editor.execute("CREATE FUNCTION helpdesk_immutable_platform_audit() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Platform audit is append-only'; END $$")
        schema_editor.execute("CREATE TRIGGER platform_audit_append_only BEFORE UPDATE OR DELETE ON tenancy_platformauditevent FOR EACH ROW EXECUTE FUNCTION helpdesk_immutable_platform_audit()")

def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor == "sqlite":
        for trigger in ("tenancy_uuid_immutable", "platform_audit_update", "platform_audit_delete"):
            schema_editor.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    elif schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("DROP TRIGGER IF EXISTS tenancy_uuid_immutable ON tenancy_tenant")
        schema_editor.execute("DROP TRIGGER IF EXISTS platform_audit_append_only ON tenancy_platformauditevent")
        schema_editor.execute("DROP FUNCTION IF EXISTS helpdesk_immutable_company_uuid()")
        schema_editor.execute("DROP FUNCTION IF EXISTS helpdesk_immutable_platform_audit()")

class Migration(migrations.Migration):
    dependencies = [("tenancy", "0002_alter_tenant_favicon_alter_tenant_logo")]
    operations = [migrations.RunPython(install, uninstall)]
