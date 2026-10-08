import uuid

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models

from .access import grant_allows, has_capability, object_matches_grant, role_allows
from .context import current_context
from .managers import TenantManager
from .models import TenantMembership


class TenantOwnedModel(models.Model):
    tenant = models.ForeignKey("tenancy.Tenant", on_delete=models.PROTECT)
    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    objects = TenantManager()
    # Only reviewed infrastructure code may use all_objects.
    all_objects = models.Manager()
    permission_resource = "settings"
    grant_paths = {}
    requester_paths = ()

    class Meta:
        abstract = True
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "id"], name="%(app_label)s_%(class)s_tenant_pk"
            )
        ]
        indexes = [models.Index(fields=["tenant", "created_at"], name="%(class)s_t_created")]

    def validate_tenant_write(self):
        ctx = current_context()
        if not ctx or not ctx.tenant.available:
            raise PermissionDenied("An authorized active company context is required.")
        if not self.tenant_id:
            self.tenant = ctx.tenant
        if self.tenant_id != ctx.tenant_id:
            raise PermissionDenied("Company context does not match the record.")
        if not has_capability(self.permission_resource, "edit", ctx=ctx):
            raise PermissionDenied("This module is disabled or your company role cannot change it.")
        if self.pk:
            previous = (
                type(self)
                .all_objects.filter(pk=self.pk)
                .values_list("tenant_id", flat=True)
                .first()
            )
            if previous is not None and previous != self.tenant_id:
                raise ValidationError("Company ownership is immutable.")
        if not role_allows(ctx, self.permission_resource, "edit") and not any(
            grant_allows(g, self.permission_resource, "edit") and object_matches_grant(self, g)
            for g in ctx.grants
        ):
            raise PermissionDenied("Your company role does not allow this change.")
        if (
            ctx.role == "USER"
            and self.pk
            and self.permission_resource == "ticket"
            and not ctx.system
        ):
            if not type(self).objects.filter(pk=self.pk).exists():
                raise PermissionDenied("This record is outside your permitted scope.")

    def clean(self):
        super().clean()
        if not self.tenant_id:
            raise ValidationError("Company is required.")
        for field in self._meta.fields:
            if isinstance(field, models.FileField):
                file = getattr(self, field.name)
                if (
                    file
                    and file._committed
                    and not file.name.startswith(f"tenants/{self.tenant.uuid}/")
                ):
                    raise ValidationError(
                        {
                            field.name: "Stored files must belong to this company's storage namespace."
                        }
                    )
        previous = type(self).all_objects.filter(pk=self.pk).first() if self.pk else None
        for field in self._meta.fields:
            if (
                not field.is_relation
                or field.name == "tenant"
                or not getattr(self, field.attname, None)
            ):
                continue
            related = field.remote_field.model
            if isinstance(related, str):
                continue
            if issubclass(related, TenantOwnedModel):
                related_tenant = (
                    related.all_objects.filter(pk=getattr(self, field.attname))
                    .values_list("tenant_id", flat=True)
                    .first()
                )
                if related_tenant != self.tenant_id:
                    raise ValidationError(
                        {field.name: "The selected record belongs to another company."}
                    )
            elif related._meta.label_lower == settings.AUTH_USER_MODEL.lower():
                if previous and getattr(previous, field.attname) == getattr(self, field.attname):
                    continue  # Retain historical actors after their membership expires.
                if (
                    not TenantMembership.objects.valid()
                    .filter(tenant_id=self.tenant_id, user_id=getattr(self, field.attname))
                    .exists()
                ):
                    ctx = current_context()
                    if not (
                        ctx
                        and ctx.actor
                        and ctx.actor.pk == getattr(self, field.attname)
                        and (
                            ctx.support
                            or (
                                ctx.grants
                                and field.name
                                in {"actor", "author", "created_by", "uploaded_by", "decided_by"}
                            )
                        )
                    ):
                        raise ValidationError(
                            {field.name: "The user needs active membership in this company."}
                        )

    def save(self, *args, **kwargs):
        self.validate_tenant_write()
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self.validate_tenant_write()
        return super().delete(*args, **kwargs)
