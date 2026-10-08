import hashlib
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.tenancy.access import has_capability, require_capability, resolve_access
from apps.tenancy.context import current_context, tenant_context
from apps.tenancy.models import TenantMembership

from .models import (
    Attachment,
    AuditEvent,
    Comment,
    DomainEvent,
    EmailOutbox,
    Notification,
    NotificationTemplate,
    ReferenceCounter,
    ServiceAccount,
    Ticket,
)


def authorized_service(*, tenant, actor):
    """Validate identity before entering a service's explicitly supplied company."""
    ctx = current_context()
    if (
        actor is None
        and ctx
        and ctx.tenant_id == tenant.pk
        and ctx.actor is None
        and (ctx.service_account or ctx.grants)
    ):
        return tenant_context(tenant, service_account=ctx.service_account, grants=ctx.grants)
    if not actor or not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied("Authentication is required.")
    if (
        ctx
        and ctx.support
        and ctx.tenant_id == tenant.pk
        and ctx.actor
        and ctx.actor.pk == actor.pk
    ):
        return tenant_context(tenant, actor=actor, role=ctx.role, grants=ctx.grants, support=True)
    membership, grants = resolve_access(actor, tenant)
    if not membership and not grants:
        raise PermissionDenied("Company membership or an explicit grant is required.")
    return tenant_context(
        tenant, actor=actor, role=membership.role if membership else "", grants=grants
    )


def record_audit(action, obj, *, metadata=None):
    ctx = current_context()
    if not ctx or obj.tenant_id != ctx.tenant_id:
        raise PermissionDenied("Audit company mismatch.")
    with tenant_context(
        ctx.tenant, actor=ctx.actor, system=True, grants=ctx.grants, support=ctx.support
    ):
        AuditEvent.objects.create(
            tenant=ctx.tenant,
            actor=ctx.actor,
            action=action,
            resource_type=obj._meta.label,
            resource_uuid=str(obj.uuid),
            metadata=metadata or {},
        )


def emit_event(event_type, obj, **payload):
    ctx = current_context()
    with tenant_context(
        ctx.tenant, actor=ctx.actor, system=True, grants=ctx.grants, support=ctx.support
    ):
        DomainEvent.objects.create(
            tenant=ctx.tenant,
            event_type=event_type,
            payload={
                "tenant_id": ctx.tenant_id,
                "resource_uuid": str(obj.uuid),
                "actor_id": getattr(ctx.actor, "pk", None),
                **payload,
            },
        )


def next_reference(*, tenant, prefix):
    ctx = current_context()
    if not ctx or ctx.tenant_id != tenant.pk:
        raise PermissionDenied("Reference company mismatch.")
    year = timezone.localdate().year
    with tenant_context(tenant, system=True), transaction.atomic():
        counter, _ = ReferenceCounter.objects.select_for_update().get_or_create(
            tenant=tenant, prefix=prefix, year=year
        )
        counter.current_value += 1
        while Ticket.objects.filter(
            reference=f"{prefix}-{year}-{counter.current_value:06d}"
        ).exists():
            counter.current_value += 1
        counter.save(update_fields=["current_value", "updated_at"])
        return f"{prefix}-{year}-{counter.current_value:06d}"


def notify_ticket(ticket, message):
    """Resolve recipients against current company membership and preferences."""
    ctx = current_context()
    ids = {ticket.requester_id, ticket.assigned_to_id} - {None}
    members = (
        TenantMembership.objects.valid()
        .filter(tenant=ticket.tenant, user_id__in=ids)
        .select_related("user")
    )
    with tenant_context(
        ticket.tenant, actor=ctx.actor, system=True, grants=ctx.grants, support=ctx.support
    ):
        template = NotificationTemplate.objects.filter(name="ticket-update").first()
        for member in members:
            if member.browser_notifications:
                Notification.objects.create(
                    user=member.user, ticket=ticket, message=f"{ticket.reference}: {message}"
                )
            if ticket.category.email_notifications and member.email_notifications:
                history = "\n".join(
                    f"{comment.created_at:%Y-%m-%d %H:%M}: {comment.body}"
                    for comment in Comment.objects.filter(ticket=ticket, internal=False)
                )
                EmailOutbox.objects.create(
                    recipient=member.user,
                    ticket=ticket,
                    subject=f"[{ticket.tenant.short_name or ticket.tenant.name}] {ticket.reference} · {message}",
                    body=f"{ticket.tenant.name}\n{ticket.reference}: {ticket.title}\n{message}\n\nWorkflow: {' → '.join(ticket.category.workflow.stages)}\n\n{history}\n\n{template.footer if template else ticket.tenant.contact_email}",
                )


@transaction.atomic
def create_ticket(*, tenant, actor, data, files=()):
    with authorized_service(tenant=tenant, actor=actor):
        require_capability("ticket", "edit")
        ctx = current_context()
        fields = {
            k: v
            for k, v in data.items()
            if k
            in {
                "title",
                "description",
                "request_type",
                "project",
                "category",
                "organization",
                "policy",
                "priority",
                "assigned_to",
            }
        }
        if ctx.role == "USER":
            fields.pop("assigned_to", None)
        category = fields["category"]
        ticket = Ticket(
            tenant=tenant,
            requester=actor,
            reference=next_reference(tenant=tenant, prefix=fields["project"].code),
            workflow_stage=category.workflow.stages[0],
            due_at=timezone.now() + timedelta(hours=category.sla.hours),
            support_group=category.support_group,
            **fields,
        )
        ticket.save()
        for file in files:
            attach_file(ticket=ticket, actor=actor, file=file)
        record_audit("ticket.created", ticket)
        emit_event("TicketCreated", ticket, ticket_id=ticket.pk)
        notify_ticket(ticket, "Ticket created")
        return ticket


def validate_upload(file):
    if file.size > settings.MAX_ATTACHMENT_BYTES:
        raise ValidationError("Files must be 10 MB or smaller.")
    if file.size == 0:
        raise ValidationError("Empty files cannot be uploaded.")
    from pathlib import Path

    if Path(file.name).suffix.lower() not in {
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".txt",
        ".csv",
        ".xlsx",
        ".docx",
        ".eml",
        ".msg",
    }:
        raise ValidationError("Use PDF, image, text, CSV, Excel, Word or email files.")


from django.conf import settings  # noqa: E402


def attach_file(*, ticket, actor, file):
    require_capability("ticket", "edit")
    validate_upload(file)
    if not Ticket.objects.filter(pk=ticket.pk).exists():
        raise PermissionDenied("Ticket access denied.")
    attachment = Attachment(
        tenant=ticket.tenant,
        ticket=ticket,
        uploaded_by=actor,
        original_name=file.name[:255],
        size=file.size,
    )
    # Validation precedes storage. If the DB write fails, remove the newly stored file.
    attachment.validate_tenant_write()
    attachment.file = file
    try:
        attachment.save()
    except Exception:
        if attachment.file and attachment.file._committed:
            attachment.file.delete(save=False)
        raise
    record_audit("attachment.uploaded", attachment)
    return attachment


@transaction.atomic
def add_comment(*, tenant, actor, ticket_uuid, body, internal=False):
    with authorized_service(tenant=tenant, actor=actor):
        require_capability("ticket", "edit")
        ticket = Ticket.objects.get(uuid=ticket_uuid)
        if not body.strip():
            raise ValidationError("Enter a comment.")
        if current_context().role == "USER":
            internal = False
        comment = Comment.objects.create(
            tenant=tenant, ticket=ticket, author=actor, body=body.strip(), internal=internal
        )
        record_audit(
            "ticket.commented",
            ticket,
            metadata={"comment_uuid": str(comment.uuid), "internal": internal},
        )
        notify_ticket(ticket, "New comment" if not internal else "Ticket updated")
        return comment


@transaction.atomic
def advance_ticket(*, tenant, actor, ticket_uuid):
    with authorized_service(tenant=tenant, actor=actor):
        require_capability("ticket", "edit")
        if current_context().role == "USER":
            raise PermissionDenied("A support role is required to advance the workflow.")
        ticket = Ticket.objects.select_for_update().get(uuid=ticket_uuid)
        if ticket.status in {"COMPLETE", "CANCELLED"}:
            raise ValidationError("This ticket is already closed.")
        workflow = ticket.category.workflow
        stage = ticket.workflow_stage
        if stage == "APPROVAL":
            approvals = ticket.approvals.all()
            if not approvals.exists() or approvals.exclude(decision="APPROVED").exists():
                raise ValidationError("All required approvals must be approved.")
        if (
            stage == "INTAKE"
            and workflow.rules.get("attachments_required")
            and not ticket.attachments.exists()
        ):
            raise ValidationError("This workflow requires an attachment.")
        if stage == "TPA" and ticket.transactions.exclude(status="COMPLETE").exists():
            raise ValidationError("Complete all linked TPA transactions first.")
        next_index = workflow.stages.index(stage) + 1
        if next_index >= len(workflow.stages):
            raise ValidationError("The workflow is already complete.")
        ticket.workflow_stage = workflow.stages[next_index]
        ticket.status = "COMPLETE" if ticket.workflow_stage == "COMPLETE" else "IN_PROGRESS"
        if ticket.status == "COMPLETE":
            ticket.completed_at = timezone.now()
        ticket.save()
        record_audit(
            "ticket.stage_changed", ticket, metadata={"from": stage, "to": ticket.workflow_stage}
        )
        emit_event("TicketStageChanged", ticket, stage=ticket.workflow_stage)
        notify_ticket(ticket, f"Moved to {ticket.workflow_stage.lower()}")
        return ticket


@transaction.atomic
def decide_approval(*, tenant, actor, approval_uuid, decision, remarks=""):
    from .models import Approval

    with authorized_service(tenant=tenant, actor=actor):
        require_capability("approval", "edit")
        approval = Approval.objects.select_for_update().get(uuid=approval_uuid)
        if approval.assigned_to_id != actor.pk and current_context().role not in {
            "ADMIN",
            "MANAGER",
        }:
            raise PermissionDenied("This approval is assigned to another user.")
        if approval.decision != "PENDING" or approval.ticket.workflow_stage != "APPROVAL":
            raise ValidationError("This approval cannot be decided in the current workflow state.")
        if decision not in {"APPROVED", "REJECTED"}:
            raise ValidationError("Choose approve or reject.")
        approval.decision, approval.remarks, approval.decided_at, approval.decided_by = (
            decision,
            remarks,
            timezone.now(),
            actor,
        )
        approval.save()
        record_audit("approval.decided", approval, metadata={"decision": decision})
        notify_ticket(approval.ticket, f"Approval {decision.lower()}")
        return approval


def authenticate_service_account(authorization):
    if not authorization.startswith("Bearer hd_"):
        return None
    digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
    # Reviewed global credential lookup: the token itself binds the company.
    account = (
        ServiceAccount.all_objects.select_related("tenant")
        .filter(token_hash=digest, active=True, tenant__active=True, tenant__status="ACTIVE")
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))
        .first()
    )
    if account:
        ServiceAccount.all_objects.filter(pk=account.pk).update(last_used_at=timezone.now())
    return account


def selection_queryset(model):
    """Reviewed read-only reference lists; never exposes credentials or configuration blobs."""
    ctx = current_context()
    if not ctx:
        return model.objects.none()
    allowed = {
        "project",
        "product",
        "category",
        "supportgroup",
        "organizationtype",
        "sla",
        "workflow",
    }
    if model._meta.model_name not in allowed:
        return model.objects.all()
    if not any(
        has_capability(resource, "edit")
        for resource in ["ticket", "policy", "organization", "settings"]
    ):
        return model.objects.none()
    return model.all_objects.filter(tenant_id=ctx.tenant_id)
