from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

from apps.tenancy.context import current_context
from apps.tenancy.models import TenantMembership

from . import models
from .services import selection_queryset, validate_upload


class ScopedModelForm(forms.ModelForm):
    """All ownership is supplied by the server; FK selectors are scoped independently."""

    def __init__(self, *args, tenant=None, **kwargs):
        super().__init__(*args, **kwargs)
        ctx = current_context()
        self.tenant = tenant or (ctx.tenant if ctx else None)
        if self.tenant:
            self.instance.tenant = self.tenant
        for name, field in self.fields.items():
            if name in {"secret_env", "token_env"} and self.tenant:
                from apps.tenancy.secrets import secret_prefix

                field.help_text = f"Environment variable prefix for this company: {secret_prefix(self.tenant)}. Leave blank for a provider that needs no token."
            field.widget.attrs["class"] = (
                "form-control"
                if not isinstance(field.widget, forms.CheckboxInput)
                else "form-check-input"
            )
            if isinstance(field.widget, forms.Textarea):
                field.widget.attrs["rows"] = 4
            if isinstance(field, forms.ModelChoiceField):
                if field.queryset.model == get_user_model():
                    ids = (
                        TenantMembership.objects.valid()
                        .filter(tenant=self.tenant)
                        .values_list("user_id", flat=True)
                    )
                    field.queryset = (
                        get_user_model()
                        .objects.filter(pk__in=ids, is_active=True)
                        .order_by("first_name", "username")
                    )
                else:
                    field.queryset = selection_queryset(field.queryset.model)
            if isinstance(field, forms.DateField):
                field.widget = forms.DateInput(attrs={"type": "date", "class": "form-control"})
            if isinstance(field, forms.DateTimeField):
                field.widget = forms.DateTimeInput(
                    attrs={"type": "datetime-local", "class": "form-control"},
                    format="%Y-%m-%dT%H:%M",
                )


class MultiFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultiFileField(forms.FileField):
    def clean(self, data, initial=None):
        files = data if isinstance(data, (list, tuple)) else [data] if data else []
        result = [super(MultiFileField, self).clean(file, initial) for file in files]
        for file in result:
            validate_upload(file)
        if len(result) > 10:
            raise ValidationError("Upload at most 10 files at a time.")
        return result


class TicketForm(ScopedModelForm):
    files = MultiFileField(widget=MultiFileInput, required=False)

    class Meta:
        model = models.Ticket
        fields = [
            "request_type",
            "title",
            "description",
            "project",
            "category",
            "organization",
            "policy",
            "priority",
            "assigned_to",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        ctx = current_context()
        if ctx and ctx.actor:
            self.instance.requester = ctx.actor
        if ctx and ctx.role == "USER":
            for field in ["assigned_to", "organization", "policy"]:
                self.fields.pop(field, None)
        category_id = self.data.get("category") or self.initial.get("category")
        if category_id and str(category_id).isdigit():
            category = selection_queryset(models.Category).filter(pk=category_id).first()
            if category:
                self.instance.workflow_stage = category.workflow.stages[0]


class PolicyForm(ScopedModelForm):
    class Meta:
        model = models.Policy
        fields = ["policy_number", "organization", "product", "start_date", "end_date", "status"]


class MemberForm(ScopedModelForm):
    class Meta:
        model = models.Member
        fields = [
            "plan",
            "member_id",
            "employee_id",
            "full_name",
            "date_of_birth",
            "gender",
            "relationship",
            "principal",
            "card_number",
            "effective_date",
            "active",
        ]

    def __init__(self, *args, policy, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.policy = policy
        self.fields["plan"].queryset = models.BenefitPlan.objects.filter(policy=policy)
        self.fields["principal"].queryset = models.Member.objects.filter(
            policy=policy, relationship="PRINCIPAL", active=True
        )


class PlanForm(ScopedModelForm):
    class Meta:
        model = models.BenefitPlan
        fields = ["code", "name", "annual_premium", "sum_assured"]

    def __init__(self, *args, policy, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.policy = policy


class OrganizationForm(ScopedModelForm):
    class Meta:
        model = models.Organization
        fields = [
            "organization_type",
            "parent",
            "code",
            "name",
            "email",
            "phone",
            "address",
            "active",
        ]


class TaskForm(ScopedModelForm):
    class Meta:
        model = models.Task
        fields = ["ticket", "title", "assigned_to", "due_at", "complete"]


class TransactionForm(ScopedModelForm):
    class Meta:
        model = models.Transaction
        fields = [
            "ticket",
            "policy",
            "member",
            "transaction_type",
            "effective_date",
            "premium_impact",
            "payload",
        ]


class DocumentForm(ScopedModelForm):
    class Meta:
        model = models.Document
        fields = ["title", "description", "organization", "policy", "file"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if current_context() and current_context().actor:
            self.instance.uploaded_by = current_context().actor

    def clean_file(self):
        file = self.cleaned_data["file"]
        if hasattr(file, "size"):
            validate_upload(file)
        self.instance.original_name = file.name[:255]
        return file


class KnowledgeForm(ScopedModelForm):
    class Meta:
        model = models.KnowledgeArticle
        fields = ["title", "category", "body", "published"]


class ApprovalForm(ScopedModelForm):
    class Meta:
        model = models.Approval
        fields = ["assigned_to"]


class CompanySettingsForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["configuration"].help_text = (
            'Optional dashboard settings: {"dashboard": {"kpis": ["open", "completed", '
            '"overdue", "active_members"], "chart_type": "bar", "default_range_days": 30}}. '
            "Chart types: bar or table. Use 0 days for all time. Permissions always apply."
        )

    class Meta:
        from apps.tenancy.models import Tenant

        model = Tenant
        fields = [
            "name",
            "short_name",
            "logo",
            "favicon",
            "contact_email",
            "contact_phone",
            "address",
            "primary_color",
            "secondary_color",
            "default_language",
            "default_timezone",
            "default_currency",
            "configuration",
            "feature_flags",
        ]


class InvitationForm(forms.Form):
    email = forms.EmailField()
    role = forms.ChoiceField(choices=TenantMembership.Role.choices)


class AIAgentForm(ScopedModelForm):
    from .agents import TOOL_SPECS

    allowed_tools = forms.MultipleChoiceField(
        choices=[(name, name.replace("_", " ").title()) for name in TOOL_SPECS],
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Select tools supported by the selected domain. The server checks the user's permissions on each call.",
    )
    allowed_roles = forms.MultipleChoiceField(
        choices=TenantMembership.Role.choices,
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Leave empty to use existing company resource permissions. Selected roles add a restriction.",
    )
    knowledge_categories = forms.CharField(
        required=False,
        max_length=1600,
        help_text="Comma-separated knowledge categories, for example Claims, Claims SOP. Empty disables knowledge search.",
    )

    class Meta:
        model = models.AIAgentConfig
        fields = [
            "code",
            "name",
            "domain",
            "provider",
            "system_prompt",
            "knowledge_categories",
            "allowed_tools",
            "allowed_roles",
            "active",
            "is_default",
            "max_steps",
            "timeout_seconds",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial["knowledge_categories"] = ", ".join(self.instance.knowledge_categories)

    def clean_knowledge_categories(self):
        return [
            v.strip() for v in self.cleaned_data["knowledge_categories"].split(",") if v.strip()
        ]
