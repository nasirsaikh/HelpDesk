from django.core.exceptions import PermissionDenied
from django.core.files.base import ContentFile
from django.core.mail import send_mail
from django.db import transaction
from django.utils import timezone

from apps.tenancy.context import current_context, tenant_context
from apps.tenancy.models import TenantMembership

from .models import EmailOutbox, Job, MailboxConfig, Ticket
from .services import authorized_service


def queue_job(*, job_type, actor, payload=None):
    from apps.tenancy.access import require_capability

    ctx = current_context()
    require_capability(
        "report" if job_type == "REPORT_EXPORT" else "settings",
        "view" if job_type == "REPORT_EXPORT" else "edit",
    )
    payload = dict(payload or {})
    payload["tenant_id"] = ctx.tenant_id
    with tenant_context(
        ctx.tenant, actor=actor, system=True, grants=ctx.grants, support=ctx.support
    ):
        return Job.objects.create(job_type=job_type, actor=actor, payload=payload)


def send_outbox(*, tenant):
    ctx = current_context()
    if not ctx or ctx.tenant_id != tenant.pk or not ctx.system:
        raise PermissionDenied("Authorized worker context is required.")
    sent = 0
    for email in EmailOutbox.objects.filter(sent_at__isnull=True, attempts__lt=3).select_related(
        "recipient", "ticket__category"
    )[:100]:
        member = (
            TenantMembership.objects.valid()
            .filter(user=email.recipient, tenant=tenant, email_notifications=True)
            .first()
        )
        if not member or not email.ticket.category.email_notifications:
            continue
        try:
            with authorized_service(tenant=tenant, actor=email.recipient):
                if not Ticket.objects.filter(pk=email.ticket_id).exists():
                    continue
            send_mail(email.subject, email.body, None, [email.recipient.email], fail_silently=False)
            email.sent_at, email.last_error = timezone.now(), ""
            sent += 1
        except Exception as exc:
            # Never log SMTP credentials, Graph tokens, private email bodies or provider response payloads.
            email.last_error = type(exc).__name__
        email.attempts += 1
        email.save()
    return sent


def run_job(*, job_id, tenant):
    """Trusted dispatcher supplies explicit tenant; every queued operation rechecks access."""
    if not tenant.available:
        return False
    with tenant_context(tenant, system=True):
        with transaction.atomic():
            job = (
                Job.objects.select_for_update()
                .filter(pk=job_id, state="QUEUED", scheduled_at__lte=timezone.now())
                .first()
            )
            if not job:
                return False
            job.state, job.started_at = "RUNNING", timezone.now()
            job.save()
        try:
            if job.payload.get("tenant_id") != tenant.pk:
                raise PermissionDenied("Job payload company mismatch.")
            tenant.refresh_from_db()
            if not tenant.available:
                raise PermissionDenied("Company is deactivated.")
            if job.job_type == "REPORT_EXPORT":
                from .exports import ticket_report

                with authorized_service(tenant=tenant, actor=job.actor):
                    if not tenant.feature_flags.get("analytics", True):
                        raise PermissionDenied("Analytics is disabled.")
                    content = ticket_report(filters=job.payload.get("filters", {}))
                    from apps.tenancy.cache import access_signature

                    job.payload["access_signature"] = access_signature()
                job.result_file.save("tickets.csv", ContentFile(content), save=False)
            elif job.job_type == "MAILBOX_SYNC":
                from .integrations import sync_mailbox

                mailbox = MailboxConfig.objects.get(pk=job.payload["mailbox_id"])
                sync_mailbox(mailbox=mailbox)
            elif job.job_type == "EMAIL_SEND":
                send_outbox(tenant=tenant)
            else:
                raise PermissionDenied("Unsupported job type.")
            job.state = "COMPLETE"
        except Exception as exc:
            job.state, job.error = "FAILED", type(exc).__name__
        job.completed_at = timezone.now()
        job.save()
        return job.state == "COMPLETE"
