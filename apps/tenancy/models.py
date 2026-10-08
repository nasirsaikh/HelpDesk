import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


def tenant_brand_path(instance, filename):
    from pathlib import Path

    return (
        f"tenants/{instance.uuid}/branding/{uuid.uuid4().hex}{Path(filename).suffix.lower()[:12]}"
    )


class TenantQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if {"uuid", "id", "pk"} & kwargs.keys():
            raise ValidationError("Company security identifiers are immutable.")
        return super().update(**kwargs)

    def delete(self):
        raise ValidationError("Archive companies instead of deleting them.")


class Tenant(models.Model):
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=160)
    legal_name = models.CharField(max_length=200, blank=True)
    short_name = models.CharField(max_length=60, blank=True)
    slug = models.SlugField(unique=True)
    active = models.BooleanField(default=True)
    status = models.CharField(
        max_length=12,
        choices=[("ACTIVE", "Active"), ("SUSPENDED", "Suspended"), ("ARCHIVED", "Archived")],
        default="ACTIVE",
    )
    logo = models.ImageField(upload_to=tenant_brand_path, blank=True)
    favicon = models.ImageField(upload_to=tenant_brand_path, blank=True)
    primary_color = models.CharField(max_length=7, default="#176b5b")
    secondary_color = models.CharField(max_length=7, default="#e8f3ef")
    default_language = models.CharField(
        max_length=8, default="en", choices=[("en", "English"), ("ar", "Arabic")]
    )
    default_timezone = models.CharField(max_length=64, default="Asia/Muscat")
    default_currency = models.CharField(max_length=3, default="OMR")
    country = models.CharField(max_length=60, blank=True)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=40, blank=True)
    address = models.TextField(blank=True)
    registration_number = models.CharField(max_length=80, blank=True)
    tax_registration_number = models.CharField(max_length=80, blank=True)
    domain = models.CharField(max_length=253, blank=True)
    configuration = models.JSONField(default=dict, blank=True)
    feature_flags = models.JSONField(default=dict, blank=True)
    subscription = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    objects = TenantQuerySet.as_manager()

    class Meta:
        ordering = ["name"]
        verbose_name = "company"
        verbose_name_plural = "companies"

    @property
    def available(self):
        return self.active and self.status == "ACTIVE"

    def clean(self):
        import re

        for field in ("primary_color", "secondary_color"):
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", getattr(self, field)):
                raise ValidationError({field: "Use a six-digit hex color."})
        for field in ("logo", "favicon"):
            file = getattr(self, field)
            if file and file._committed and not file.name.startswith(f"tenants/{self.uuid}/"):
                raise ValidationError({field: "Branding files must belong to this company."})
        try:
            ZoneInfo(self.default_timezone)
        except ZoneInfoNotFoundError:
            raise ValidationError(
                {"default_timezone": "Use an IANA timezone, for example Asia/Muscat."}
            )
        for field in ("configuration", "feature_flags", "subscription"):
            if not isinstance(getattr(self, field), dict):
                raise ValidationError({field: "A JSON object is required."})
        if any(not isinstance(value, bool) for value in self.feature_flags.values()):
            raise ValidationError({"feature_flags": "Feature flag values must be true or false."})
        dashboard = self.configuration.get("dashboard", {})
        if not isinstance(dashboard, dict) or set(dashboard) - {
            "kpis",
            "chart_type",
            "default_range_days",
        }:
            raise ValidationError({"configuration": "Use supported dashboard configuration keys."})
        kpis = dashboard.get("kpis", ["open", "completed", "overdue", "active_members"])
        if (
            not isinstance(kpis, list)
            or any(
                not isinstance(key, str)
                or key not in {"open", "completed", "overdue", "active_members"}
                for key in kpis
            )
            or len(kpis) != len(set(kpis))
        ):
            raise ValidationError(
                {"configuration": "Dashboard KPIs must be unique supported names."}
            )
        if dashboard.get("chart_type", "bar") not in ("bar", "table"):
            raise ValidationError({"configuration": "Dashboard chart_type must be bar or table."})
        days = dashboard.get("default_range_days", 0)
        if type(days) is not int or not 0 <= days <= 3650:
            raise ValidationError(
                {"configuration": "Dashboard default_range_days must be 0–3650; 0 means all time."}
            )

    def save(self, *args, **kwargs):
        if self.pk:
            previous = Tenant.objects.get(pk=self.pk)
            if previous.uuid != self.uuid:
                raise ValidationError("Company UUID is immutable.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(
            "Archive a company instead. Data destruction requires a retention workflow."
        )

    def __str__(self):
        return self.name


class ActiveMembershipQuerySet(models.QuerySet):
    def valid(self, at=None):
        at = at or timezone.now()
        return self.filter(
            active=True, tenant__active=True, tenant__status="ACTIVE", user__is_active=True
        ).filter(
            Q(valid_from__isnull=True) | Q(valid_from__lte=at),
            Q(valid_until__isnull=True) | Q(valid_until__gt=at),
        )


class TenantMembership(models.Model):
    class Role(models.TextChoices):
        ADMIN = "ADMIN", "Administrator"
        MANAGER = "MANAGER", "Operations manager"
        AGENT = "AGENT", "Support agent"
        USER = "USER", "Requester"
        AUDITOR = "AUDITOR", "Auditor (read only)"

    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, related_name="memberships")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="tenant_memberships"
    )
    role = models.CharField(max_length=12, choices=Role.choices, default=Role.USER)
    active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)
    joined_at = models.DateTimeField(default=timezone.now)
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    employee_number = models.CharField(max_length=40, blank=True)
    department = models.CharField(max_length=80, blank=True)
    job_title = models.CharField(max_length=80, blank=True)
    language = models.CharField(max_length=8, blank=True)
    email_notifications = models.BooleanField(default=True)
    browser_notifications = models.BooleanField(default=True)
    configuration = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_tenant_memberships",
    )
    objects = ActiveMembershipQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["tenant", "user"], name="unique_tenant_user"),
            models.UniqueConstraint(
                fields=["user"], condition=Q(is_default=True), name="one_default_company"
            ),
        ]

    def clean(self):
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValidationError("Membership end must be after its start.")
        from .context import current_context

        ctx = current_context()
        if ctx and not ctx.system:
            if ctx.tenant_id != self.tenant_id or ctx.role != "ADMIN":
                raise ValidationError("Company administrator access is required.")
        if (
            self.pk
            and TenantMembership.objects.filter(pk=self.pk)
            .exclude(tenant_id=self.tenant_id)
            .exists()
        ):
            raise ValidationError("Membership company cannot change.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user} / {self.tenant} / {self.get_role_display()}"


class TenantAccessGrant(models.Model):
    """Platform-managed exceptions. All supplied resource filters are conjunctive."""

    source_tenant = models.ForeignKey(
        Tenant, on_delete=models.PROTECT, related_name="issued_access_grants"
    )
    target_tenant = models.ForeignKey(
        Tenant, on_delete=models.PROTECT, related_name="received_access_grants"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True
    )
    group = models.ForeignKey("desk.SupportGroup", on_delete=models.PROTECT, null=True, blank=True)
    service_account = models.ForeignKey(
        "desk.ServiceAccount", on_delete=models.PROTECT, null=True, blank=True
    )
    scopes = models.JSONField(default=list)
    resource_scope = models.JSONField(default=dict, blank=True)
    active = models.BooleanField(default=True)
    valid_from = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField(null=True, blank=True)
    reason = models.TextField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="created_access_grants"
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="approved_access_grants"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        from .access import GRANT_SCOPES

        if sum(bool(v) for v in [self.user_id, self.group_id, self.service_account_id]) != 1:
            raise ValidationError(
                "Choose exactly one user, source company group, or service account."
            )
        if self.source_tenant_id == self.target_tenant_id:
            raise ValidationError("Use membership for access inside the same company.")
        if (
            not isinstance(self.scopes, list)
            or not self.scopes
            or set(self.scopes) - set(GRANT_SCOPES)
        ):
            raise ValidationError({"scopes": "Choose supported permission scopes."})
        if not isinstance(self.resource_scope, dict) or set(self.resource_scope) - {
            "project",
            "organization",
            "policy",
            "ticket",
            "product",
        }:
            raise ValidationError(
                {
                    "resource_scope": "Supported restrictions: project, organization, policy, ticket, product."
                }
            )
        from django.apps import apps

        for key, value in self.resource_scope.items():
            model = apps.get_model(
                "desk",
                {
                    "project": "Project",
                    "organization": "Organization",
                    "policy": "Policy",
                    "ticket": "Ticket",
                    "product": "Product",
                }[key],
            )
            if (
                not isinstance(value, int)
                or not model.all_objects.filter(pk=value, tenant_id=self.target_tenant_id).exists()
            ):
                raise ValidationError(
                    {"resource_scope": "Resource restrictions must reference the target company."}
                )
        if self.valid_until and self.valid_until <= self.valid_from:
            raise ValidationError("Grant expiry must follow its start.")
        if self.group_id and self.group.tenant_id != self.source_tenant_id:
            raise ValidationError("Group must belong to the source company.")
        if self.service_account_id and self.service_account.tenant_id != self.source_tenant_id:
            raise ValidationError("Service account must belong to the source company.")
        if (
            self.user_id
            and not TenantMembership.objects.valid()
            .filter(tenant_id=self.source_tenant_id, user_id=self.user_id)
            .exists()
        ):
            raise ValidationError("User must be an active member of the source company.")
        if not self.created_by.platform_operator or not self.approved_by.platform_operator:
            raise ValidationError("Access grants require platform approval.")
        if not self.reason.strip():
            raise ValidationError("An access reason is required.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class SupportSession(models.Model):
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    reason = models.TextField()
    reference = models.CharField(max_length=100, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)


class PlatformAuditEvent(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.PROTECT, null=True, blank=True)
    action = models.CharField(max_length=80)
    reason = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
