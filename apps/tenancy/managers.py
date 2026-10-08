from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models, transaction

from .access import scoped_queryset
from .context import current_context


class TenantQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if "tenant" in kwargs or "tenant_id" in kwargs:
            raise ValidationError("Company ownership is immutable.")
        # Validate changed relationships and capabilities even on bulk mutations.
        with transaction.atomic():
            for obj in self:
                for key, value in kwargs.items():
                    if hasattr(value, "resolve_expression"):
                        raise ValidationError("Use a domain service for expression updates.")
                    setattr(obj, key, value)
                obj.validate_tenant_write()
                obj.full_clean()
            return super().update(**kwargs)

    def bulk_create(self, objs, **kwargs):
        if kwargs.get("ignore_conflicts") or kwargs.get("update_conflicts"):
            raise ValidationError("Conflict-skipping bulk writes are not supported.")
        for obj in objs:
            obj.validate_tenant_write()
            obj.full_clean()
        return super().bulk_create(objs, **kwargs)

    def bulk_update(self, objs, fields, **kwargs):
        if "tenant" in fields or "tenant_id" in fields:
            raise ValidationError("Company ownership is immutable.")
        with transaction.atomic():
            for obj in objs:
                obj.save(update_fields=fields)
        return len(objs)

    def delete(self):
        for obj in self:
            obj.validate_tenant_write()
        return super().delete()

    def raw(self, *args, **kwargs):
        raise PermissionDenied("Raw SQL is not available on company-scoped managers.")

    def extra(self, *args, **kwargs):
        raise PermissionDenied("SQL fragments are not available on company-scoped managers.")


class TenantManager(models.Manager.from_queryset(TenantQuerySet)):
    def get_queryset(self):
        return scoped_queryset(super().get_queryset(), current_context())

    def for_tenant(self, tenant):
        ctx = current_context()
        if not ctx or ctx.tenant_id != tenant.pk:
            return super().get_queryset().none()
        return self.get_queryset()
