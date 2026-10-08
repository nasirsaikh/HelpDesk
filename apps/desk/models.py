import hashlib
import secrets
import uuid
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from apps.tenancy.base import TenantOwnedModel


def private_upload_path(instance, filename):
    if not instance.tenant_id:
        raise ValidationError("A company is required before storing a file.")
    suffix = Path(filename).suffix.lower()[:12]
    return f"tenants/{instance.tenant.uuid}/{instance._meta.model_name}/{uuid.uuid4().hex}{suffix}"


class OrganizationType(TenantOwnedModel):
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=100)

    class Meta(TenantOwnedModel.Meta):
        abstract = False
        constraints = TenantOwnedModel.Meta.constraints + [
            models.UniqueConstraint(fields=["tenant", "code"], name="org_type_company_code")
        ]

    def __str__(self):
        return self.name


class Organization(TenantOwnedModel):
    permission_resource = "organization"
    grant_paths = {"organization": "id"}
    organization_type = models.ForeignKey(OrganizationType, on_delete=models.PROTECT)
    parent = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True)
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=160)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    address = models.TextField(blank=True)
    active = models.BooleanField(default=True)

    class Meta(TenantOwnedModel.Meta):
        abstract = False
        ordering = ["name"]
        constraints = TenantOwnedModel.Meta.constraints + [
            models.UniqueConstraint(fields=["tenant", "code"], name="organization_company_code")
        ]

    def clean(self):
        super().clean()
        node, seen = self.parent if self.parent_id else None, {self.pk} if self.pk else set()
        while node:
            if node.pk in seen:
                raise ValidationError({"parent": "Organization hierarchy cannot contain a cycle."})
            seen.add(node.pk)
            node = node.parent

    def __str__(self):
        return self.name


class SupportGroup(TenantOwnedModel):
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=100)
    members = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through="SupportGroupMember",
        related_name="company_support_groups",
    )

    class Meta(TenantOwnedModel.Meta):
        abstract = False
        constraints = TenantOwnedModel.Meta.constraints + [
            models.UniqueConstraint(fields=["tenant", "code"], name="support_group_company_code")
        ]

    def __str__(self):
        return self.name


class SupportGroupMember(TenantOwnedModel):
    group = models.ForeignKey(SupportGroup, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    active = models.BooleanField(default=True)

    class Meta(TenantOwnedModel.Meta):
        abstract = False
        constraints = TenantOwnedModel.Meta.constraints + [
            models.UniqueConstraint(
                fields=["tenant", "group", "user"], name="unique_company_group_member"
            )
        ]


class Project(TenantOwnedModel):
    code = models.CharField(max_length=16)
    name = models.CharField(max_length=100)
    active = models.BooleanField(default=True)

    class Meta(TenantOwnedModel.Meta):
        abstract = False
        constraints = TenantOwnedModel.Meta.constraints + [
            models.UniqueConstraint(fields=["tenant", "code"], name="project_company_code")
        ]

    def __str__(self):
        return f"{self.code} · {self.name}"


class Product(TenantOwnedModel):
    project = models.ForeignKey(Project, on_delete=models.PROTECT)
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=100)

    class Meta(TenantOwnedModel.Meta):
        abstract = False
        constraints = TenantOwnedModel.Meta.constraints + [
            models.UniqueConstraint(fields=["tenant", "code"], name="product_company_code")
        ]

    def __str__(self):
        return self.name


class SLA(TenantOwnedModel):
    name = models.CharField(max_length=100)
    hours = models.PositiveIntegerField(default=40, validators=[MinValueValidator(1)])

    class Meta(TenantOwnedModel.Meta):
        abstract = False
        constraints = TenantOwnedModel.Meta.constraints + [
            models.UniqueConstraint(fields=["tenant", "name"], name="sla_company_name")
        ]

    def __str__(self):
        return f"{self.name} ({self.hours} hours)"


DEFAULT_STAGES = ["INTAKE", "VALIDATION", "APPROVAL", "TPA", "COMPLETE"]


def default_stages():
    return DEFAULT_STAGES.copy()


class Workflow(TenantOwnedModel):
    name = models.CharField(max_length=100)
    stages = models.JSONField(default=default_stages)
    rules = models.JSONField(default=dict, blank=True)

    class Meta(TenantOwnedModel.Meta):
        abstract = False
        constraints = TenantOwnedModel.Meta.constraints + [
            models.UniqueConstraint(fields=["tenant", "name"], name="workflow_company_name")
        ]

    def clean(self):
        super().clean()
        if (
            not isinstance(self.stages, list)
            or not self.stages
            or len(set(self.stages)) != len(self.stages)
            or set(self.stages) - set(DEFAULT_STAGES)
            or self.stages[-1] != "COMPLETE"
        ):
            raise ValidationError({"stages": "Use unique supported stages ending with COMPLETE."})
        allowed = {
            "backdating_days",
            "auto_approval_amount",
            "tpa_required",
            "card_required",
            "attachments_required",
        }
        if not isinstance(self.rules, dict) or set(self.rules) - allowed:
            raise ValidationError({"rules": "Only named, supported business rules are accepted."})
        for key in ("tpa_required", "card_required", "attachments_required"):
            if key in self.rules and not isinstance(self.rules[key], bool):
                raise ValidationError({"rules": f"{key} must be a boolean."})
        for key in ("backdating_days", "auto_approval_amount"):
            if key in self.rules and (
                not isinstance(self.rules[key], (int, float)) or self.rules[key] < 0
            ):
                raise ValidationError({"rules": f"{key} must be nonnegative."})

    def __str__(self):
        return self.name


class Category(TenantOwnedModel):
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=100)
    support_group = models.ForeignKey(SupportGroup, on_delete=models.PROTECT, null=True, blank=True)
    sla = models.ForeignKey(SLA, on_delete=models.PROTECT)
    workflow = models.ForeignKey(Workflow, on_delete=models.PROTECT)
    email_notifications = models.BooleanField(default=False)

    class Meta(TenantOwnedModel.Meta):
        abstract = False
        constraints = TenantOwnedModel.Meta.constraints + [
            models.UniqueConstraint(
                fields=["tenant", "product", "code"], name="category_company_product_code"
            )
        ]

    def __str__(self):
        return self.name


class ReferenceCounter(TenantOwnedModel):
    permission_resource = "internal"
    prefix = models.CharField(max_length=16)
    year = models.PositiveIntegerField()
    current_value = models.PositiveBigIntegerField(default=0)

    class Meta(TenantOwnedModel.Meta):
        abstract = False
        constraints = TenantOwnedModel.Meta.constraints + [
            models.UniqueConstraint(
                fields=["tenant", "prefix", "year"], name="reference_company_prefix_year"
            )
        ]


POLICY_PATHS = {
    "policy": "id",
    "organization": "organization_id",
    "product": "product_id",
    "project": "product__project_id",
}


class Policy(TenantOwnedModel):
    permission_resource = "policy"
    grant_paths = POLICY_PATHS
    policy_number = models.CharField(max_length=80)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(
        max_length=12,
        choices=[("ACTIVE", "Active"), ("INACTIVE", "Inactive"), ("DRAFT", "Draft")],
        default="DRAFT",
    )

    class Meta(TenantOwnedModel.Meta):
        abstract = False
        ordering = ["-created_at"]
        constraints = TenantOwnedModel.Meta.constraints + [
            models.UniqueConstraint(
                fields=["tenant", "policy_number"], name="policy_company_number"
            )
        ]

    def clean(self):
        super().clean()
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "Policy end cannot precede its start."})

    def __str__(self):
        return self.policy_number


CHILD_POLICY_PATHS = {
    "policy": "policy_id",
    "organization": "policy__organization_id",
    "product": "policy__product_id",
    "project": "policy__product__project_id",
}


class BenefitPlan(TenantOwnedModel):
    permission_resource = "policy"
    grant_paths = CHILD_POLICY_PATHS
    policy = models.ForeignKey(Policy, on_delete=models.PROTECT, related_name="plans")
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=100)
    annual_premium = models.DecimalField(
        max_digits=14, decimal_places=3, default=0, validators=[MinValueValidator(0)]
    )
    sum_assured = models.DecimalField(
        max_digits=14, decimal_places=3, default=0, validators=[MinValueValidator(0)]
    )

    class Meta(TenantOwnedModel.Meta):
        abstract = False
        constraints = TenantOwnedModel.Meta.constraints + [
            models.UniqueConstraint(
                fields=["tenant", "policy", "code"], name="plan_company_policy_code"
            )
        ]

    def __str__(self):
        return f"{self.policy} · {self.name}"


class Member(TenantOwnedModel):
    permission_resource = "policy"
    grant_paths = CHILD_POLICY_PATHS
    policy = models.ForeignKey(Policy, on_delete=models.PROTECT, related_name="members")
    plan = models.ForeignKey(BenefitPlan, on_delete=models.PROTECT)
    member_id = models.CharField(max_length=40)
    employee_id = models.CharField(max_length=40, blank=True)
    full_name = models.CharField(max_length=160)
    date_of_birth = models.DateField()
    gender = models.CharField(
        max_length=12, choices=[("MALE", "Male"), ("FEMALE", "Female"), ("OTHER", "Other")]
    )
    relationship = models.CharField(
        max_length=12,
        choices=[
            ("PRINCIPAL", "Principal"),
            ("SPOUSE", "Spouse"),
            ("CHILD", "Child"),
            ("OTHER", "Other"),
        ],
        default="PRINCIPAL",
    )
    principal = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="dependents"
    )
    active = models.BooleanField(default=True)
    card_number = models.CharField(max_length=80, blank=True)
    effective_date = models.DateField(null=True, blank=True)

    class Meta(TenantOwnedModel.Meta):
        abstract = False
        constraints = TenantOwnedModel.Meta.constraints + [
            models.UniqueConstraint(
                fields=["tenant", "policy", "member_id"], name="member_company_policy_id"
            )
        ]

    def clean(self):
        super().clean()
        if self.plan_id and self.plan.policy_id != self.policy_id:
            raise ValidationError({"plan": "Select a plan under this policy."})
        if self.date_of_birth and self.date_of_birth > timezone.localdate():
            raise ValidationError({"date_of_birth": "Birth date cannot be in the future."})
        if self.relationship != "PRINCIPAL" and not self.principal_id:
            raise ValidationError({"principal": "A dependent needs a principal member."})
        if self.principal_id and (
            self.principal_id == self.pk
            or self.principal.policy_id != self.policy_id
            or self.principal.relationship != "PRINCIPAL"
        ):
            raise ValidationError({"principal": "Select a principal in the same policy."})

    def __str__(self):
        return f"{self.full_name} · {self.member_id}"


TICKET_PATHS = {
    "ticket": "id",
    "project": "project_id",
    "policy": "policy_id",
    "organization": "organization_id",
    "product": "category__product_id",
}


class Ticket(TenantOwnedModel):
    permission_resource = "ticket"
    grant_paths = TICKET_PATHS
    requester_paths = ("requester_id", "assigned_to_id")
    reference = models.CharField(max_length=50)
    title = models.CharField(max_length=200)
    description = models.TextField()
    request_type = models.CharField(
        max_length=16,
        choices=[
            ("TICKET", "Ticket"),
            ("POLICY", "Policy enrollment"),
            ("ENDORSEMENT", "Endorsement"),
            ("CLAIM", "Claim"),
        ],
        default="TICKET",
    )
    project = models.ForeignKey(Project, on_delete=models.PROTECT)
    category = models.ForeignKey(Category, on_delete=models.PROTECT)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, null=True, blank=True)
    policy = models.ForeignKey(Policy, on_delete=models.PROTECT, null=True, blank=True)
    requester = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="requested_tickets"
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="assigned_tickets",
    )
    support_group = models.ForeignKey(SupportGroup, on_delete=models.PROTECT, null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=[
            ("OPEN", "Open"),
            ("IN_PROGRESS", "In progress"),
            ("WAITING", "Waiting"),
            ("COMPLETE", "Complete"),
            ("CANCELLED", "Cancelled"),
        ],
        default="OPEN",
    )
    priority = models.CharField(
        max_length=8,
        choices=[("LOW", "Low"), ("NORMAL", "Normal"), ("HIGH", "High"), ("URGENT", "Urgent")],
        default="NORMAL",
    )
    workflow_stage = models.CharField(max_length=16, default="INTAKE")
    due_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantOwnedModel.Meta):
        abstract = False
        ordering = ["-created_at"]
        constraints = TenantOwnedModel.Meta.constraints + [
            models.UniqueConstraint(fields=["tenant", "reference"], name="ticket_company_reference")
        ]
        indexes = TenantOwnedModel.Meta.indexes + [
            models.Index(fields=["tenant", "status"], name="ticket_tenant_status")
        ]

    def clean(self):
        super().clean()
        flag = {"CLAIM": "claims", "POLICY": "policies", "ENDORSEMENT": "tpa"}.get(
            self.request_type, "tickets"
        )
        if not self.tenant.feature_flags.get(flag, True):
            raise ValidationError(
                {"request_type": "This request module is disabled for the company."}
            )
        if (
            self.category_id
            and self.project_id
            and self.category.product.project_id != self.project_id
        ):
            raise ValidationError({"category": "Category must belong to the selected project."})
        if (
            self.policy_id
            and self.organization_id
            and self.policy.organization_id != self.organization_id
        ):
            raise ValidationError({"organization": "Organization must match the policy holder."})
        if self.category_id and self.workflow_stage not in self.category.workflow.stages:
            raise ValidationError(
                {"workflow_stage": "This stage is not part of the category workflow."}
            )

    def __str__(self):
        return self.reference


CHILD_TICKET_PATHS = {
    key: "ticket_id" if path == "id" else "ticket__" + path for key, path in TICKET_PATHS.items()
}


class Comment(TenantOwnedModel):
    permission_resource = "ticket"
    grant_paths = CHILD_TICKET_PATHS
    requester_paths = ("ticket__requester_id", "ticket__assigned_to_id")
    ticket = models.ForeignKey(Ticket, on_delete=models.PROTECT, related_name="comments")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    body = models.TextField()
    internal = models.BooleanField(default=False)

    class Meta(TenantOwnedModel.Meta):
        abstract = False
        ordering = ["created_at"]


class Task(TenantOwnedModel):
    permission_resource = "task"
    grant_paths = CHILD_TICKET_PATHS
    ticket = models.ForeignKey(Ticket, on_delete=models.PROTECT, related_name="tasks")
    title = models.CharField(max_length=160)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True
    )
    due_at = models.DateTimeField(null=True, blank=True)
    complete = models.BooleanField(default=False)

    def __str__(self):
        return self.title


class Approval(TenantOwnedModel):
    permission_resource = "approval"
    grant_paths = CHILD_TICKET_PATHS
    ticket = models.ForeignKey(Ticket, on_delete=models.PROTECT, related_name="approvals")
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    decision = models.CharField(
        max_length=12,
        choices=[("PENDING", "Pending"), ("APPROVED", "Approved"), ("REJECTED", "Rejected")],
        default="PENDING",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approval_decisions",
    )
    remarks = models.TextField(blank=True)


class Transaction(TenantOwnedModel):
    permission_resource = "transaction"
    grant_paths = {**CHILD_TICKET_PATHS, "policy": "policy_id"}
    ticket = models.ForeignKey(Ticket, on_delete=models.PROTECT, related_name="transactions")
    policy = models.ForeignKey(Policy, on_delete=models.PROTECT)
    member = models.ForeignKey(Member, on_delete=models.PROTECT, null=True, blank=True)
    transaction_type = models.CharField(
        max_length=24,
        choices=[
            ("MEMBER_ADD", "Member addition"),
            ("MEMBER_DELETE", "Member deletion"),
            ("TERMINATION", "Termination"),
            ("DEMOGRAPHIC_CHANGE", "Demographic change"),
            ("CLAIM", "Claim"),
            ("POLICY_ENROLL", "Policy enrollment"),
        ],
    )
    effective_date = models.DateField()
    status = models.CharField(
        max_length=16,
        default="DRAFT",
        choices=[
            ("DRAFT", "Draft"),
            ("APPROVAL", "Approval"),
            ("TPA", "TPA processing"),
            ("COMPLETE", "Complete"),
            ("REJECTED", "Rejected"),
        ],
    )
    premium_impact = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    payload = models.JSONField(default=dict, blank=True)

    def clean(self):
        super().clean()
        if self.transaction_type == "CLAIM" and not self.tenant.feature_flags.get("claims", True):
            raise ValidationError({"transaction_type": "Claims are disabled for this company."})
        if self.ticket_id and self.ticket.policy_id and self.ticket.policy_id != self.policy_id:
            raise ValidationError({"policy": "Policy must match the linked ticket."})
        if self.member_id and self.member.policy_id != self.policy_id:
            raise ValidationError({"member": "Member must belong to the selected policy."})
        if self.ticket_id and self.effective_date:
            days = self.ticket.category.workflow.rules.get("backdating_days", 30)
            if (timezone.localdate() - self.effective_date).days > days:
                raise ValidationError(
                    {"effective_date": "Effective date exceeds this company's backdating limit."}
                )

    def __str__(self):
        return f"{self.ticket} · {self.get_transaction_type_display()}"


class Document(TenantOwnedModel):
    permission_resource = "document"
    grant_paths = {"policy": "policy_id", "organization": "organization_id"}
    title = models.CharField(max_length=160)
    description = models.TextField(blank=True)
    policy = models.ForeignKey(Policy, on_delete=models.PROTECT, null=True, blank=True)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, null=True, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    file = models.FileField(upload_to=private_upload_path)
    original_name = models.CharField(max_length=255)

    def __str__(self):
        return self.title


class Attachment(TenantOwnedModel):
    permission_resource = "ticket"
    grant_paths = CHILD_TICKET_PATHS
    requester_paths = ("ticket__requester_id", "ticket__assigned_to_id")
    ticket = models.ForeignKey(Ticket, on_delete=models.PROTECT, related_name="attachments")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    file = models.FileField(upload_to=private_upload_path)
    original_name = models.CharField(max_length=255)
    size = models.PositiveIntegerField()


class KnowledgeArticle(TenantOwnedModel):
    permission_resource = "knowledge"
    title = models.CharField(max_length=160)
    body = models.TextField()
    published = models.BooleanField(default=False)
    category = models.CharField(max_length=80, blank=True)

    def __str__(self):
        return self.title


class PlatformArticle(models.Model):
    """Explicitly public platform manuals; company users cannot mutate these."""

    title = models.CharField(max_length=160)
    body = models.TextField()
    published = models.BooleanField(default=False)

    def __str__(self):
        return self.title


class Notification(TenantOwnedModel):
    permission_resource = "notification"
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    ticket = models.ForeignKey(Ticket, on_delete=models.PROTECT, null=True, blank=True)
    message = models.CharField(max_length=255)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantOwnedModel.Meta):
        abstract = False
        ordering = ["-created_at"]


class AuditEvent(TenantOwnedModel):
    permission_resource = "audit"
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    action = models.CharField(max_length=80)
    resource_type = models.CharField(max_length=80)
    resource_uuid = models.CharField(max_length=36, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta(TenantOwnedModel.Meta):
        abstract = False
        ordering = ["-created_at"]

    def validate_tenant_write(self):
        super().validate_tenant_write()
        if self.pk:
            raise ValidationError("Audit events are append-only.")

    def delete(self, *args, **kwargs):
        raise ValidationError("Audit events are append-only.")


class DomainEvent(TenantOwnedModel):
    permission_resource = "internal"
    event_type = models.CharField(max_length=80)
    payload = models.JSONField(default=dict)
    processed_at = models.DateTimeField(null=True, blank=True)


class MailboxConfig(TenantOwnedModel):
    name = models.CharField(max_length=100)
    email_address = models.EmailField()
    active = models.BooleanField(default=True)
    graph_directory_id = models.CharField(max_length=80, blank=True)
    graph_client_id = models.CharField(max_length=80, blank=True)
    secret_env = models.CharField(max_length=80, blank=True)
    cursor = models.CharField(max_length=2048, blank=True)

    def clean(self):
        super().clean()
        from apps.tenancy.secrets import validate_secret_reference

        try:
            validate_secret_reference(self.secret_env, self.tenant)
        except ValidationError as exc:
            raise ValidationError({"secret_env": exc.messages})

    def __str__(self):
        return self.name


class InboundEmail(TenantOwnedModel):
    permission_resource = "mailbox"
    mailbox = models.ForeignKey(MailboxConfig, on_delete=models.PROTECT)
    message_id = models.CharField(max_length=255)
    sender = models.EmailField()
    subject = models.CharField(max_length=255)
    body = models.TextField(blank=True)
    received_at = models.DateTimeField(default=timezone.now)
    state = models.CharField(
        max_length=20,
        default="NEW",
        choices=[
            ("NEW", "New"),
            ("NEEDS_REVIEW", "Needs review"),
            ("UNAUTHORIZED", "Unauthorized"),
            ("PROCESSED", "Processed"),
        ],
    )
    policy = models.ForeignKey(Policy, on_delete=models.PROTECT, null=True, blank=True)
    extracted_payload = models.JSONField(default=dict, blank=True)

    class Meta(TenantOwnedModel.Meta):
        abstract = False
        constraints = TenantOwnedModel.Meta.constraints + [
            models.UniqueConstraint(
                fields=["tenant", "mailbox", "message_id"], name="email_company_mailbox_message"
            )
        ]

    def __str__(self):
        return self.subject


class EmailAuthority(TenantOwnedModel):
    email_address = models.EmailField()
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    policy = models.ForeignKey(Policy, on_delete=models.PROTECT, null=True, blank=True)
    permitted_transaction_types = models.JSONField(default=list)
    active = models.BooleanField(default=True)
    valid_from = models.DateField(null=True, blank=True)
    valid_until = models.DateField(null=True, blank=True)

    def clean(self):
        super().clean()
        if self.policy_id and self.policy.organization_id != self.organization_id:
            raise ValidationError({"policy": "Policy and organization must agree."})
        allowed = {choice[0] for choice in Transaction._meta.get_field("transaction_type").choices}
        if (
            not isinstance(self.permitted_transaction_types, list)
            or set(self.permitted_transaction_types) - allowed
        ):
            raise ValidationError(
                {"permitted_transaction_types": "Use supported transaction type codes."}
            )


class AIProviderConfig(TenantOwnedModel):
    name = models.CharField(max_length=100)
    provider = models.CharField(
        max_length=16,
        choices=[
            ("OLLAMA", "Ollama"),
            ("OPENAI_COMPAT", "OpenAI compatible"),
            ("HUGGINGFACE", "Hugging Face"),
        ],
        default="OLLAMA",
    )
    endpoint = models.URLField(default="http://127.0.0.1:11434")
    model = models.CharField(max_length=160)
    token_env = models.CharField(max_length=80, blank=True)
    active = models.BooleanField(default=True)
    supports_vision = models.BooleanField(default=False)
    allow_sensitive_data = models.BooleanField(default=False)
    timeout_seconds = models.PositiveIntegerField(default=60, validators=[MinValueValidator(1)])

    def clean(self):
        super().clean()
        from apps.tenancy.secrets import validate_secret_reference

        try:
            validate_secret_reference(self.token_env, self.tenant)
        except ValidationError as exc:
            raise ValidationError({"token_env": exc.messages})
        if self.timeout_seconds > 180:
            raise ValidationError({"timeout_seconds": "Maximum timeout is 180 seconds."})

    def __str__(self):
        return self.name


class AIPrompt(TenantOwnedModel):
    purpose = models.CharField(max_length=40, default="EXTRACTION")
    transaction_type = models.CharField(max_length=24, blank=True)
    prompt = models.TextField()
    training_examples = models.JSONField(default=list, blank=True)

    class Meta(TenantOwnedModel.Meta):
        abstract = False
        constraints = TenantOwnedModel.Meta.constraints + [
            models.UniqueConstraint(
                fields=["tenant", "purpose", "transaction_type"],
                name="ai_prompt_company_purpose_type",
            )
        ]


class AIAgentConfig(TenantOwnedModel):
    """A company-owned model binding, instructions and a subset of registered tools."""

    from .agent_specs import DOMAINS

    code = models.CharField(max_length=40)
    name = models.CharField(max_length=100)
    domain = models.CharField(max_length=16, choices=list(DOMAINS.items()))
    provider = models.ForeignKey(AIProviderConfig, on_delete=models.PROTECT, null=True, blank=True)
    system_prompt = models.TextField(max_length=8000)
    knowledge_categories = models.JSONField(
        default=list,
        blank=True,
        help_text="Knowledge categories this agent may search. An empty list disables knowledge retrieval.",
    )
    allowed_tools = models.JSONField(
        default=list,
        blank=True,
        help_text="A subset of the registered tools for this agent's domain.",
    )
    allowed_roles = models.JSONField(
        default=list,
        blank=True,
        help_text="Optional company role restriction. Empty means use existing resource permissions.",
    )
    active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False)
    max_steps = models.PositiveSmallIntegerField(default=4)
    timeout_seconds = models.PositiveSmallIntegerField(default=120)

    class Meta(TenantOwnedModel.Meta):
        abstract = False
        verbose_name = "AI agent"
        verbose_name_plural = "AI agents"
        ordering = ["name"]
        constraints = TenantOwnedModel.Meta.constraints + [
            models.UniqueConstraint(fields=["tenant", "code"], name="agent_company_code"),
            models.UniqueConstraint(
                fields=["tenant", "domain"],
                condition=models.Q(is_default=True),
                name="agent_company_default_domain",
            ),
        ]

    def clean(self):
        import re

        from apps.tenancy.models import TenantMembership

        from .agent_specs import DOMAIN_TOOLS

        super().clean()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,39}", self.code):
            raise ValidationError({"code": "Use 2–40 uppercase letters, digits or underscores."})
        for field, allowed in [
            ("allowed_tools", DOMAIN_TOOLS.get(self.domain, set())),
            ("allowed_roles", set(TenantMembership.Role.values)),
        ]:
            values = getattr(self, field)
            if (
                not isinstance(values, list)
                or any(not isinstance(v, str) or v not in allowed for v in values)
                or len(values) != len(set(values))
            ):
                raise ValidationError({field: "Use unique supported values for this domain."})
        values = self.knowledge_categories
        if (
            not isinstance(values, list)
            or len(values) > 20
            or any(not isinstance(v, str) or not v.strip() or len(v) > 80 for v in values)
        ):
            raise ValidationError(
                {
                    "knowledge_categories": "Use at most 20 nonempty category names, each at most 80 characters."
                }
            )
        if not 1 <= self.max_steps <= 8 or not 10 <= self.timeout_seconds <= 180:
            raise ValidationError("Use 1–8 model steps and a 10–180 second time budget.")

    def __str__(self):
        return self.name


class AIAgentRun(TenantOwnedModel):
    permission_resource = "internal"
    agent = models.ForeignKey(AIAgentConfig, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    support_session_uuid = models.UUIDField(null=True, blank=True, editable=False)
    user_input = models.TextField(max_length=3000)
    access_signature = models.TextField(blank=True)
    resources = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=12,
        default="QUEUED",
        choices=[
            ("QUEUED", "Queued"),
            ("RUNNING", "Running"),
            ("COMPLETE", "Complete"),
            ("FAILED", "Failed"),
        ],
    )
    provider_name = models.CharField(max_length=100, blank=True)
    model_name = models.CharField(max_length=160, blank=True)
    prompt_hash = models.CharField(max_length=64, blank=True)
    answer = models.TextField(blank=True)
    trace = models.JSONField(default=list, blank=True)
    error = models.CharField(max_length=200, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantOwnedModel.Meta):
        abstract = False
        verbose_name = "AI agent request"
        verbose_name_plural = "AI agent requests"
        ordering = ["-created_at"]


class AIAgentAction(TenantOwnedModel):
    permission_resource = "internal"
    run = models.ForeignKey(AIAgentRun, on_delete=models.PROTECT, related_name="actions")
    ticket = models.ForeignKey(Ticket, on_delete=models.PROTECT)
    body = models.TextField(max_length=2000)
    draft_hash = models.CharField(max_length=64)
    applied_at = models.DateTimeField(null=True, blank=True)
    dismissed_at = models.DateTimeField(null=True, blank=True)

    class Meta(TenantOwnedModel.Meta):
        abstract = False
        verbose_name = "AI comment draft"
        verbose_name_plural = "AI comment drafts"
        constraints = TenantOwnedModel.Meta.constraints + [
            models.UniqueConstraint(fields=["run", "draft_hash"], name="agent_run_unique_draft")
        ]


class VectorDocument(TenantOwnedModel):
    permission_resource = "knowledge"
    article = models.ForeignKey(KnowledgeArticle, on_delete=models.PROTECT)
    content = models.TextField()
    embedding = models.JSONField(default=list, blank=True)
    namespace = models.CharField(max_length=80, editable=False)

    def clean(self):
        super().clean()
        self.namespace = f"tenant_{self.tenant.uuid.hex}"


class NotificationTemplate(TenantOwnedModel):
    name = models.CharField(max_length=100)
    subject = models.CharField(max_length=160, default="Ticket update")
    footer = models.TextField(blank=True)

    class Meta(TenantOwnedModel.Meta):
        abstract = False
        constraints = TenantOwnedModel.Meta.constraints + [
            models.UniqueConstraint(fields=["tenant", "name"], name="notification_template_company")
        ]


class EmailOutbox(TenantOwnedModel):
    permission_resource = "internal"
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    ticket = models.ForeignKey(Ticket, on_delete=models.PROTECT)
    subject = models.CharField(max_length=255)
    body = models.TextField()
    sent_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=160, blank=True)


class ServiceAccount(TenantOwnedModel):
    name = models.CharField(max_length=100)
    active = models.BooleanField(default=True)
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    scopes = models.JSONField(default=list)
    expires_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)

    def clean(self):
        super().clean()
        allowed = {
            f"{resource}.{action}"
            for resource in ["ticket", "policy", "knowledge", "report", "document", "transaction"]
            for action in ["view", "edit"]
        }
        if not isinstance(self.scopes, list) or not self.scopes or set(self.scopes) - allowed:
            raise ValidationError({"scopes": "Use explicit resource.view or resource.edit scopes."})

    @staticmethod
    def new_token():
        token = "hd_" + secrets.token_urlsafe(40)
        return token, hashlib.sha256(token.encode()).hexdigest()

    def __str__(self):
        return self.name


class Job(TenantOwnedModel):
    permission_resource = "internal"
    job_type = models.CharField(
        max_length=24,
        choices=[
            ("MAILBOX_SYNC", "Mailbox sync"),
            ("REPORT_EXPORT", "Report export"),
            ("EMAIL_SEND", "Send queued email"),
        ],
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True
    )
    payload = models.JSONField(default=dict, blank=True)
    state = models.CharField(
        max_length=12,
        choices=[
            ("QUEUED", "Queued"),
            ("RUNNING", "Running"),
            ("COMPLETE", "Complete"),
            ("FAILED", "Failed"),
        ],
        default="QUEUED",
    )
    result_file = models.FileField(upload_to=private_upload_path, blank=True)
    error = models.CharField(max_length=160, blank=True)
    scheduled_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    def clean(self):
        super().clean()
        if not isinstance(self.payload, dict) or self.payload.get("tenant_id") != self.tenant_id:
            raise ValidationError({"payload": "Every job payload must carry its owning tenant_id."})


class Invitation(TenantOwnedModel):
    email = models.EmailField()
    role = models.CharField(
        max_length=12,
        choices=[
            ("ADMIN", "Administrator"),
            ("MANAGER", "Manager"),
            ("AGENT", "Agent"),
            ("USER", "Requester"),
            ("AUDITOR", "Auditor"),
        ],
        default="USER",
    )
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)
