"""Trusted single-company offboarding export; never expose as a general portal API."""

import json
import os
import tempfile
import zipfile
from pathlib import Path

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.utils import timezone

from apps.tenancy.models import PlatformAuditEvent, Tenant, TenantMembership


class Command(BaseCommand):
    help = "Export one company's records and optional private files for infrastructure offboarding."

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True)
        parser.add_argument("--output", required=True)
        parser.add_argument("--include-files", action="store_true")

    def handle(self, **options):
        tenant = Tenant.objects.filter(code=options["company"]).first()
        if not tenant:
            raise CommandError("Company not found.")
        target = Path(options["output"]).resolve()
        if target.exists():
            raise CommandError("The output already exists; use a new filename.")
        target.parent.mkdir(parents=True, exist_ok=True)
        files = {}

        def collect_files(obj):
            for field in obj._meta.fields:
                if isinstance(field, models.FileField):
                    file = getattr(obj, field.name)
                    if file:
                        if not file.name.startswith(f"tenants/{tenant.uuid}/"):
                            raise CommandError(
                                "A stored file is outside the company's namespace; export stopped."
                            )
                        files[file.name] = file

        company = Tenant.objects.filter(pk=tenant.pk).values().get()
        payload = {"company": company, "exported_at": timezone.now(), "models": {}}
        if options["include_files"]:
            collect_files(tenant)
        # Reviewed infrastructure managers: every dataset is filtered by exactly this company.
        for model in apps.get_app_config("desk").get_models():
            if not hasattr(model, "all_objects"):
                continue
            fields = [field.attname for field in model._meta.fields if field.name != "token_hash"]
            queryset = model.all_objects.filter(tenant=tenant)
            payload["models"][model._meta.label] = list(queryset.values(*fields))
            if options["include_files"]:
                for obj in queryset:
                    collect_files(obj)
        memberships = TenantMembership.objects.filter(tenant=tenant)
        payload["memberships"] = list(memberships.values())
        payload["users"] = list(
            get_user_model()
            .objects.filter(pk__in=memberships.values("user_id"))
            .values("id", "email", "username", "first_name", "last_name", "is_active")
        )
        payload["platform_audit"] = list(PlatformAuditEvent.objects.filter(tenant=tenant).values())
        # A complete ZIP is linked into place atomically, without replacing an existing export.
        with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".zip") as temporary:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    "company.json", json.dumps(payload, cls=DjangoJSONEncoder, indent=2)
                )
                for name, file in files.items():
                    try:
                        with file.open("rb") as source:
                            archive.writestr("files/" + name, source.read())
                    except FileNotFoundError as exc:
                        raise CommandError(
                            "A private file is missing; restore storage before exporting."
                        ) from exc
            temporary.flush()
            try:
                os.link(temporary.name, target)
            except FileExistsError as exc:
                raise CommandError("The output already exists; use a new filename.") from exc
        PlatformAuditEvent.objects.create(
            tenant=tenant,
            action="company.exported",
            metadata={
                "entrypoint": "management command",
                "files_included": options["include_files"],
            },
        )
        self.stdout.write(self.style.SUCCESS(f"Company {tenant.code} exported to {target}"))
