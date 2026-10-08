from django.contrib import admin
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied

from .context import current_context
from .models import PlatformAuditEvent, SupportSession, Tenant, TenantAccessGrant, TenantMembership

if admin.site.is_registered(Group):
    admin.site.unregister(Group)


class PlatformOnlyAdmin(admin.ModelAdmin):
    def has_module_permission(self, request):
        return request.user.platform_operator

    def has_view_permission(self, request, obj=None):
        return request.user.platform_operator

    has_add_permission = has_view_permission
    has_change_permission = has_view_permission
    has_delete_permission = has_view_permission

    def get_queryset(self, request):
        return (
            super().get_queryset(request)
            if request.user.platform_operator
            else super().get_queryset(request).none()
        )

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        model = db_field.remote_field.model
        if hasattr(model, "all_objects") and request.user.platform_operator:
            kwargs["queryset"] = model.all_objects.all()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        if not request.user.platform_operator:
            raise PermissionDenied
        super().save_model(request, obj, form, change)
        PlatformAuditEvent.objects.create(
            actor=request.user,
            tenant=obj
            if isinstance(obj, Tenant)
            else getattr(obj, "tenant", None) or getattr(obj, "target_tenant", None),
            action="platform.configuration_changed",
            metadata={"model": obj._meta.label, "id": obj.pk},
        )


@admin.register(Tenant)
class CompanyAdmin(PlatformOnlyAdmin):
    list_display = ["code", "name", "active", "status", "default_currency"]
    search_fields = ["code", "name"]
    readonly_fields = ["uuid", "created_at", "updated_at"]

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(TenantAccessGrant)
class AccessGrantAdmin(PlatformOnlyAdmin):
    list_display = ["source_tenant", "target_tenant", "user", "active", "valid_until"]
    readonly_fields = ["created_at"]


@admin.register(TenantMembership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ["tenant", "user", "role", "active", "is_default"]

    def has_module_permission(self, request):
        ctx = current_context()
        return request.user.platform_operator or bool(ctx and ctx.role == "ADMIN")

    def has_view_permission(self, request, obj=None):
        if request.user.platform_operator:
            return True
        ctx = current_context()
        return bool(ctx and ctx.role == "ADMIN" and (obj is None or obj.tenant_id == ctx.tenant_id))

    has_add_permission = has_view_permission
    has_change_permission = has_view_permission

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.platform_operator and not current_context():
            return qs
        ctx = current_context()
        return qs.filter(tenant_id=ctx.tenant_id) if ctx and ctx.role == "ADMIN" else qs.none()

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        ctx = current_context()
        if not request.user.platform_operator and ctx:
            if db_field.name == "tenant":
                kwargs["queryset"] = Tenant.objects.filter(pk=ctx.tenant_id)
            elif db_field.name in {"user", "created_by"}:
                from django.contrib.auth import get_user_model

                kwargs["queryset"] = get_user_model().objects.filter(
                    tenant_memberships__tenant_id=ctx.tenant_id, tenant_memberships__active=True
                )
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        PlatformAuditEvent.objects.create(
            actor=request.user,
            tenant=obj.tenant,
            action="membership.admin_changed",
            metadata={"user_id": obj.user_id},
        )


@admin.register(PlatformAuditEvent, SupportSession)
class PlatformHistoryAdmin(PlatformOnlyAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
