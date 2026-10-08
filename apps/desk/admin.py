from django.contrib import admin
from django.core.exceptions import PermissionDenied

from apps.tenancy.access import has_capability
from apps.tenancy.admin import PlatformOnlyAdmin
from apps.tenancy.context import current_context

from . import models as m
from .forms import ScopedModelForm
from .services import record_audit


class CompanyModelAdmin(admin.ModelAdmin):
    form = ScopedModelForm
    readonly_fields = ["tenant", "uuid", "created_at", "updated_at"]
    list_per_page = 30

    def has_module_permission(self, request):
        ctx = current_context()
        return bool(ctx and ctx.role == "ADMIN" and has_capability(self.model.permission_resource))

    def has_view_permission(self, request, obj=None):
        ctx = current_context()
        return bool(
            ctx
            and ctx.role == "ADMIN"
            and has_capability(self.model.permission_resource)
            and (obj is None or self.model.objects.filter(pk=obj.pk).exists())
        )

    def has_add_permission(self, request):
        ctx = current_context()
        return bool(
            ctx and ctx.role == "ADMIN" and has_capability(self.model.permission_resource, "edit")
        )

    def has_change_permission(self, request, obj=None):
        return self.has_view_permission(request, obj) and has_capability(
            self.model.permission_resource, "edit"
        )

    def has_delete_permission(self, request, obj=None):
        return False  # Preserve business history; portal state changes replace generic deletion.

    def save_model(self, request, obj, form, change):
        if not self.has_change_permission(request, obj if change else None):
            raise PermissionDenied
        obj.tenant = request.tenant
        super().save_model(request, obj, form, change)
        record_audit("admin.saved", obj)


for model in [
    m.OrganizationType,
    m.Organization,
    m.SupportGroup,
    m.SupportGroupMember,
    m.Project,
    m.Product,
    m.Category,
    m.SLA,
    m.Workflow,
    m.Policy,
    m.BenefitPlan,
    m.Member,
    m.Comment,
    m.Task,
    m.Document,
    m.Attachment,
    m.KnowledgeArticle,
    m.EmailAuthority,
    m.AIProviderConfig,
    m.AIPrompt,
    m.NotificationTemplate,
    m.VectorDocument,
]:
    admin.site.register(model, CompanyModelAdmin)


@admin.register(m.Ticket)
class TicketAdmin(CompanyModelAdmin):
    readonly_fields = CompanyModelAdmin.readonly_fields + [
        "reference",
        "requester",
        "status",
        "workflow_stage",
        "completed_at",
    ]

    def has_add_permission(self, request):
        return False


@admin.register(m.Approval)
class ApprovalAdmin(CompanyModelAdmin):
    readonly_fields = CompanyModelAdmin.readonly_fields + [
        "decision",
        "decided_at",
        "decided_by",
        "remarks",
    ]


@admin.register(m.Transaction)
class TransactionAdmin(CompanyModelAdmin):
    readonly_fields = CompanyModelAdmin.readonly_fields + ["status"]


@admin.register(m.MailboxConfig)
class MailboxAdmin(CompanyModelAdmin):
    readonly_fields = CompanyModelAdmin.readonly_fields + ["cursor"]


@admin.register(m.AuditEvent, m.InboundEmail, m.Notification)
class CompanyHistoryAdmin(CompanyModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(m.PlatformArticle)
class PlatformArticleAdmin(PlatformOnlyAdmin):
    pass
