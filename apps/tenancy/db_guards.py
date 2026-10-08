"""Defense in depth for tenant-consistent FKs and immutable ownership.

Run through migrations using historical models, including in SQLite development/tests.
PostgreSQL uses composite foreign keys backed by each model's (tenant_id,id) constraint.
"""


def install_guards(apps, schema_editor):
    connection = schema_editor.connection
    quote = schema_editor.quote_name
    for model in apps.get_app_config("desk").get_models():
        if not any(field.name == "tenant" for field in model._meta.fields):
            continue
        table = model._meta.db_table
        relations = [
            field
            for field in model._meta.fields
            if field.is_relation
            and field.name != "tenant"
            and any(related.name == "tenant" for related in field.remote_field.model._meta.fields)
        ]
        if connection.vendor == "sqlite":
            for field in relations:
                other = field.remote_field.model._meta.db_table
                for operation in ["INSERT", "UPDATE"]:
                    trigger = f"{table}_{field.column}_tenant_{operation.lower()}"
                    schema_editor.execute(
                        f"CREATE TRIGGER {quote(trigger)} BEFORE {operation} ON {quote(table)} "
                        f"WHEN NEW.{quote(field.column)} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM {quote(other)} "
                        f"WHERE id = NEW.{quote(field.column)} AND tenant_id = NEW.tenant_id) "
                        "BEGIN SELECT RAISE(ABORT, 'Cross-company foreign key rejected'); END"
                    )
            schema_editor.execute(
                f"CREATE TRIGGER {quote(table + '_tenant_immutable')} BEFORE UPDATE OF tenant_id ON {quote(table)} "
                "WHEN OLD.tenant_id != NEW.tenant_id BEGIN SELECT RAISE(ABORT, 'Company ownership is immutable'); END"
            )
        elif connection.vendor == "postgresql":
            for field in relations:
                other = field.remote_field.model._meta.db_table
                constraint = f"{table}_{field.column}_tenant_fk"[:63]
                schema_editor.execute(
                    f"ALTER TABLE {quote(table)} ADD CONSTRAINT {quote(constraint)} "
                    f"FOREIGN KEY (tenant_id, {quote(field.column)}) REFERENCES {quote(other)} (tenant_id, id) DEFERRABLE INITIALLY DEFERRED"
                )
            schema_editor.execute(
                "CREATE OR REPLACE FUNCTION helpdesk_immutable_company() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN IF OLD.tenant_id <> NEW.tenant_id THEN RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Company ownership is immutable'; END IF; RETURN NEW; END $$"
            )
            schema_editor.execute(
                f"CREATE TRIGGER {quote(table + '_tenant_immutable')} BEFORE UPDATE OF tenant_id ON {quote(table)} "
                "FOR EACH ROW EXECUTE FUNCTION helpdesk_immutable_company()"
            )
    audit = apps.get_model("desk", "AuditEvent")._meta.db_table
    if connection.vendor == "sqlite":
        for operation in ["UPDATE", "DELETE"]:
            schema_editor.execute(
                f"CREATE TRIGGER {quote(audit + '_append_only_' + operation.lower())} BEFORE {operation} ON {quote(audit)} "
                "BEGIN SELECT RAISE(ABORT, 'Audit events are append-only'); END"
            )
    elif connection.vendor == "postgresql":
        schema_editor.execute(
            "CREATE OR REPLACE FUNCTION helpdesk_immutable_audit() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Audit events are append-only'; END $$"
        )
        schema_editor.execute(
            f"CREATE TRIGGER {quote(audit + '_append_only')} BEFORE UPDATE OR DELETE ON {quote(audit)} "
            "FOR EACH ROW EXECUTE FUNCTION helpdesk_immutable_audit()"
        )


def remove_guards(apps, schema_editor):
    quote = schema_editor.quote_name
    if schema_editor.connection.vendor == "sqlite":
        cursor = schema_editor.connection.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'desk_%'")
        for (name,) in cursor.fetchall():
            schema_editor.execute(f"DROP TRIGGER IF EXISTS {quote(name)}")
    elif schema_editor.connection.vendor == "postgresql":
        for model in apps.get_app_config("desk").get_models():
            if not any(field.name == "tenant" for field in model._meta.fields):
                continue
            table = model._meta.db_table
            schema_editor.execute(
                f"DROP TRIGGER IF EXISTS {quote(table + '_tenant_immutable')} ON {quote(table)}"
            )
            for field in model._meta.fields:
                if field.is_relation and field.name != "tenant":
                    schema_editor.execute(
                        f"ALTER TABLE {quote(table)} DROP CONSTRAINT IF EXISTS {quote((table + '_' + field.column + '_tenant_fk')[:63])}"
                    )
        audit = apps.get_model("desk", "AuditEvent")._meta.db_table
        schema_editor.execute(
            f"DROP TRIGGER IF EXISTS {quote(audit + '_append_only')} ON {quote(audit)}"
        )
        schema_editor.execute("DROP FUNCTION IF EXISTS helpdesk_immutable_company()")
        schema_editor.execute("DROP FUNCTION IF EXISTS helpdesk_immutable_audit()")
