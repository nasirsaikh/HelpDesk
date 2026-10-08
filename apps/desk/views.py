import hashlib
import json
from datetime import timedelta
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.forms import modelform_factory
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.tenancy.access import has_capability, require_capability
from apps.tenancy.cache import tenant_cache_key
from apps.tenancy.context import current_context, tenant_context
from apps.tenancy.models import TenantMembership
from apps.tenancy.services import create_invitation

from . import forms as f
from . import models as m
from .ai import execute_analytics, retrieve_knowledge
from .exports import ticket_report
from .jobs import queue_job
from .services import add_comment, advance_ticket, create_ticket, decide_approval, record_audit


def protected(resource=None, *, feature=None, action="view"):
    def decorate(view):
        @login_required
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.tenant:
                return redirect("tenancy:select")
            if feature and not request.tenant.feature_flags.get(feature, True):
                raise Http404("Module unavailable.")
            if resource:
                require_capability(resource, action)
            return view(request, *args, **kwargs)

        return wrapper

    return decorate


@protected()
def dashboard(request):
    configuration = request.tenant.configuration.get("dashboard", {})
    days = configuration.get("default_range_days", 0)
    if "range" in request.GET:
        try:
            days = int(request.GET["range"])
        except ValueError:
            raise Http404("Dashboard range must be a number of days.")
        if not 0 <= days <= 3650:
            raise Http404("Dashboard range is outside supported limits.")
    tickets = m.Ticket.objects.all()
    if days:
        tickets = tickets.filter(created_at__gte=timezone.now() - timedelta(days=days))
    config_key = hashlib.sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()[:16]
    key = tenant_cache_key("dashboard", extra=f"{config_key}:{days}")
    stats = cache.get(key)
    if stats is None:
        stats = {
            "open": tickets.exclude(status__in=["COMPLETE", "CANCELLED"]).count(),
            "completed": tickets.filter(status="COMPLETE").count(),
            "overdue": tickets.filter(due_at__lt=timezone.now())
            .exclude(status__in=["COMPLETE", "CANCELLED"])
            .count(),
            "active_members": m.Member.objects.filter(active=True).count(),
        }
        cache.set(key, stats, 15)
    counts = list(tickets.order_by("status").values("status").annotate(total=Count("id")))
    metrics = {
        "open": (
            "Open requests",
            "Awaiting resolution",
            "↗",
            "ticket",
            reverse("desk:ticket-list"),
        ),
        "completed": (
            "Completed",
            "Closed successfully",
            "✓",
            "ticket",
            reverse("desk:ticket-list") + "?status=COMPLETE",
        ),
        "overdue": ("SLA overdue", "Need your attention", "◷", "ticket", ""),
        "active_members": (
            "Active members",
            "Across accessible policies",
            "♧",
            "policy",
            reverse("desk:policy-list"),
        ),
    }
    cards = []
    for metric in configuration.get("kpis", list(metrics)):
        label, description, icon, resource, url = metrics[metric]
        if has_capability(resource):
            cards.append(
                {
                    "key": metric,
                    "label": label,
                    "description": description,
                    "icon": icon,
                    "url": url,
                    "value": stats[metric],
                }
            )
    return render(
        request,
        "desk/dashboard.html",
        {
            "stats": stats,
            "kpi_cards": cards,
            "range_days": days,
            "range_options": sorted({0, 7, 30, 90, 365, days}),
            "chart_type": configuration.get("chart_type", "bar"),
            "recent_tickets": tickets.select_related("requester", "category")[:8],
            "status_counts": counts,
            "ticket_total": sum(row["total"] for row in counts) or 1,
            "pending_approvals": m.Approval.objects.filter(
                assigned_to=request.user, decision="PENDING"
            ).count(),
        },
    )


@protected("ticket", feature="tickets")
def ticket_list(request):
    tickets = m.Ticket.objects.select_related("requester", "assigned_to", "category")
    query = request.GET.get("q", "").strip()[:200]
    if query:
        tickets = tickets.filter(
            Q(title__icontains=query)
            | Q(reference__icontains=query)
            | Q(description__icontains=query)
        )
    if request.GET.get("status"):
        tickets = tickets.filter(status=request.GET["status"])
    page = Paginator(tickets, 20).get_page(request.GET.get("page"))
    template = (
        "partials/ticket_table.html"
        if request.headers.get("HX-Request")
        else "desk/ticket_list.html"
    )
    return render(
        request,
        template,
        {"page": page, "query": query, "statuses": m.Ticket._meta.get_field("status").choices},
    )


@protected("ticket", feature="tickets", action="edit")
def ticket_create(request):
    form = f.TicketForm(request.POST or None, request.FILES or None, tenant=request.tenant)
    if request.method == "POST" and form.is_valid():
        try:
            ticket = create_ticket(
                tenant=request.tenant,
                actor=request.user,
                data=form.cleaned_data,
                files=form.cleaned_data["files"],
            )
            messages.success(request, f"{ticket.reference} created.")
            return redirect("desk:ticket-detail", uuid=ticket.uuid)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(
        request,
        "desk/form.html",
        {
            "form": form,
            "title": "Create request",
            "subtitle": "Choose a request type, project and category. Your company supplies the workflow and SLA.",
            "cancel_url": reverse("desk:ticket-list"),
        },
    )


@protected("ticket", feature="tickets")
def ticket_detail(request, uuid):
    ticket = get_object_or_404(
        m.Ticket.objects.select_related(
            "category__workflow", "category__sla", "requester", "assigned_to", "project"
        ),
        uuid=uuid,
    )
    if request.method == "POST":
        if not has_capability("ticket", "edit"):
            raise PermissionDenied
        try:
            if request.POST.get("action") == "comment":
                add_comment(
                    tenant=request.tenant,
                    actor=request.user,
                    ticket_uuid=uuid,
                    body=request.POST.get("body", ""),
                    internal=request.POST.get("internal") == "on",
                )
            elif request.POST.get("action") == "advance":
                advance_ticket(tenant=request.tenant, actor=request.user, ticket_uuid=uuid)
            else:
                raise ValidationError("Unknown ticket action.")
            return redirect("desk:ticket-detail", uuid=uuid)
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    stages = ticket.category.workflow.stages
    current = stages.index(ticket.workflow_stage)
    return render(
        request,
        "desk/ticket_detail.html",
        {
            "ticket": ticket,
            "steps": [
                {
                    "name": stage.title(),
                    "state": "done"
                    if index < current
                    else "current"
                    if index == current
                    else "future",
                }
                for index, stage in enumerate(stages)
            ],
            "comments": m.Comment.objects.filter(ticket=ticket).select_related("author"),
            "attachments": m.Attachment.objects.filter(ticket=ticket),
            "approvals": m.Approval.objects.filter(ticket=ticket).select_related("assigned_to"),
            "transactions": m.Transaction.objects.filter(ticket=ticket),
            "can_advance": has_capability("ticket", "edit")
            and current_context().role != "USER"
            and ticket.status not in {"COMPLETE", "CANCELLED"},
            "can_comment": has_capability("ticket", "edit"),
            "can_approve": has_capability("approval", "edit"),
            "can_add_approval": has_capability("approval", "edit")
            and current_context().role in {"ADMIN", "MANAGER"},
        },
    )


@protected("approval", feature="tickets", action="edit")
def approval_create(request, uuid):
    if current_context().role not in {"ADMIN", "MANAGER"}:
        raise PermissionDenied("A company manager must select approvers.")
    ticket = get_object_or_404(m.Ticket.objects, uuid=uuid)
    form = f.ApprovalForm(request.POST or None, tenant=request.tenant)
    form.instance.ticket = ticket
    if request.method == "POST" and form.is_valid():
        approval = form.save()
        record_audit("approval.created", approval)
        return redirect("desk:ticket-detail", uuid=uuid)
    return render(
        request,
        "desk/form.html",
        {
            "form": form,
            "title": "Assign approval",
            "cancel_url": reverse("desk:ticket-detail", args=[uuid]),
        },
    )


@protected("approval", feature="tickets", action="edit")
@require_POST
def approval_decide(request, uuid):
    approval = get_object_or_404(m.Approval.objects, uuid=uuid)
    try:
        decide_approval(
            tenant=request.tenant,
            actor=request.user,
            approval_uuid=uuid,
            decision=request.POST.get("decision"),
            remarks=request.POST.get("remarks", ""),
        )
        messages.success(request, "Approval decision recorded.")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return redirect("desk:ticket-detail", uuid=approval.ticket.uuid)


@protected("ticket", feature="tickets", action="edit")
@require_POST
def attachment_upload(request, uuid):
    from .services import attach_file

    ticket = get_object_or_404(m.Ticket.objects, uuid=uuid)
    try:
        if "file" not in request.FILES:
            raise ValidationError("Select a file.")
        attach_file(ticket=ticket, actor=request.user, file=request.FILES["file"])
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return redirect("desk:ticket-detail", uuid=uuid)


@protected()
def download(request, kind, uuid):
    if kind not in {"attachment", "document"}:
        raise Http404
    feature = "tickets" if kind == "attachment" else "documents"
    if not request.tenant.feature_flags.get(feature, True):
        raise Http404
    model = m.Attachment if kind == "attachment" else m.Document
    obj = get_object_or_404(model.objects, uuid=uuid)
    if not obj.file.name.startswith(f"tenants/{request.tenant.uuid}/"):
        raise Http404
    # Protected URL is the only delivery path. Never expose FileField.url in the portal.
    record_audit("file.downloaded", obj)
    try:
        response = FileResponse(obj.file.open("rb"), as_attachment=True, filename=obj.original_name)
    except FileNotFoundError:
        raise Http404
    response["X-Content-Type-Options"] = "nosniff"
    return response


REGISTRY = {
    "policy": (
        m.Policy,
        f.PolicyForm,
        "Policies",
        "policies",
        ["policy_number", "organization", "status", "start_date", "end_date"],
    ),
    "organization": (
        m.Organization,
        f.OrganizationForm,
        "Organizations",
        "organizations",
        ["code", "name", "organization_type", "active"],
    ),
    "task": (
        m.Task,
        f.TaskForm,
        "Tasks",
        "tasks",
        ["title", "ticket", "assigned_to", "due_at", "complete"],
    ),
    "transaction": (
        m.Transaction,
        f.TransactionForm,
        "Endorsements & claims",
        "tpa",
        ["ticket", "policy", "transaction_type", "effective_date", "status", "premium_impact"],
    ),
    "document": (
        m.Document,
        f.DocumentForm,
        "Documents",
        "documents",
        ["title", "policy", "original_name", "uploaded_by"],
    ),
    "knowledge": (
        m.KnowledgeArticle,
        f.KnowledgeForm,
        "Knowledge",
        "knowledge",
        ["title", "category", "published"],
    ),
    "audit": (
        m.AuditEvent,
        None,
        "Audit trail",
        "audit",
        ["created_at", "actor", "action", "resource_type", "resource_uuid"],
    ),
}


def registry_entry(request, kind, action="view"):
    if kind not in REGISTRY:
        raise Http404
    entry = REGISTRY[kind]
    if not request.tenant.feature_flags.get(entry[3], True):
        raise Http404
    require_capability(entry[0].permission_resource, action)
    return entry


@protected()
def catalog_list(request, kind):
    model, form, title, feature, columns = registry_entry(request, kind)
    objects = model.objects.all()
    if kind == "knowledge" and not has_capability("knowledge", "edit"):
        objects = objects.filter(published=True)
    q = request.GET.get("q", "").strip()[:200]
    if q:
        search = Q(pk__in=[])
        for field in columns:
            model_field = model._meta.get_field(field)
            if model_field.get_internal_type() in {"CharField", "TextField", "EmailField"}:
                search |= Q(**{f"{field}__icontains": q})
        objects = objects.filter(search)
    page = Paginator(objects.order_by("-created_at"), 20).get_page(request.GET.get("page"))
    rows = [{"object": obj, "cells": [getattr(obj, field) for field in columns]} for obj in page]
    return render(
        request,
        "desk/catalog.html",
        {
            "title": title,
            "kind": kind,
            "rows": rows,
            "page": page,
            "columns": [field.replace("_", " ").title() for field in columns],
            "query": q,
            "can_edit": bool(form and has_capability(model.permission_resource, "edit")),
            "platform_articles": m.PlatformArticle.objects.filter(published=True)
            if kind == "knowledge"
            else [],
        },
    )


@protected()
def catalog_form(request, kind, uuid=None):
    model, form_class, title, feature, _ = registry_entry(request, kind, "edit")
    if not form_class:
        raise Http404
    instance = get_object_or_404(model.objects, uuid=uuid) if uuid else None
    form = form_class(
        request.POST or None, request.FILES or None, tenant=request.tenant, instance=instance
    )
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            obj = form.save()
            record_audit(f"{kind}.{'updated' if instance else 'created'}", obj)
            if kind == "knowledge":
                with tenant_context(request.tenant, actor=request.user, system=True):
                    vector, _ = m.VectorDocument.objects.get_or_create(
                        article=obj,
                        defaults={
                            "content": obj.body,
                            "namespace": f"tenant_{request.tenant.uuid.hex}",
                        },
                    )
                    vector.content = obj.body
                    vector.save()
        messages.success(request, "Changes saved.")
        if kind == "policy":
            return redirect("desk:policy-detail", uuid=obj.uuid)
        return redirect(f"desk:{kind}-list")
    return render(
        request,
        "desk/form.html",
        {
            "form": form,
            "title": f"{'Edit' if instance else 'Create'} {model._meta.verbose_name}",
            "cancel_url": reverse(f"desk:{kind}-list"),
        },
    )


@protected("policy", feature="policies")
def policy_detail(request, uuid):
    policy = get_object_or_404(
        m.Policy.objects.select_related("organization", "product"), uuid=uuid
    )
    members = m.Member.objects.filter(policy=policy)
    active = request.GET.get("tab", "active") != "inactive"
    return render(
        request,
        "desk/policy_detail.html",
        {
            "policy": policy,
            "plans": m.BenefitPlan.objects.filter(policy=policy),
            "active_count": members.filter(active=True).count(),
            "inactive_count": members.filter(active=False).count(),
            "members": members.filter(active=active),
            "active_tab": active,
            "tickets": m.Ticket.objects.filter(policy=policy),
            "transactions": m.Transaction.objects.filter(policy=policy),
            "can_edit": has_capability("policy", "edit"),
        },
    )


@protected("policy", feature="policies", action="edit")
def policy_child_form(request, uuid, child, member_uuid=None):
    policy = get_object_or_404(m.Policy.objects, uuid=uuid)
    if child not in {"plan", "member"}:
        raise Http404
    instance = (
        get_object_or_404(m.Member.objects, uuid=member_uuid, policy=policy)
        if member_uuid
        else None
    )
    form = (f.PlanForm if child == "plan" else f.MemberForm)(
        request.POST or None, tenant=request.tenant, policy=policy, instance=instance
    )
    if request.method == "POST" and form.is_valid():
        obj = form.save()
        record_audit(f"{child}.saved", obj)
        return redirect("desk:policy-detail", uuid=uuid)
    return render(
        request,
        "desk/form.html",
        {
            "form": form,
            "title": f"{'Edit' if instance else 'Add'} {child}",
            "subtitle": str(policy),
            "cancel_url": reverse("desk:policy-detail", args=[uuid]),
        },
    )


@protected("transaction", feature="tpa", action="edit")
@require_POST
@transaction.atomic
def transaction_complete(request, uuid):
    obj = get_object_or_404(m.Transaction.objects.select_for_update(), uuid=uuid)
    try:
        if obj.status in {"COMPLETE", "REJECTED"} or obj.ticket.workflow_stage != "TPA":
            raise ValidationError(
                "Transaction can only complete during the linked ticket's TPA stage."
            )
        if (
            obj.ticket.category.workflow.rules.get("card_required")
            and obj.member_id
            and not obj.member.card_number
        ):
            raise ValidationError("A member card number is required.")
        if (
            obj.transaction_type in {"MEMBER_ADD", "MEMBER_DELETE", "TERMINATION"}
            and not obj.member_id
        ):
            raise ValidationError("Link the member before completing this transaction.")
        obj.status = "COMPLETE"
        obj.save()
        # The completed transaction authorizes only its own linked member/ticket side effects.
        ctx = current_context()
        with tenant_context(
            request.tenant, actor=request.user, system=True, grants=ctx.grants, support=ctx.support
        ):
            if obj.member_id and obj.transaction_type in {
                "MEMBER_ADD",
                "MEMBER_DELETE",
                "TERMINATION",
            }:
                obj.member.active = obj.transaction_type == "MEMBER_ADD"
                obj.member.save()
            if (
                not m.Transaction.objects.filter(ticket=obj.ticket)
                .exclude(status="COMPLETE")
                .exists()
            ):
                obj.ticket.workflow_stage, obj.ticket.status, obj.ticket.completed_at = (
                    "COMPLETE",
                    "COMPLETE",
                    timezone.now(),
                )
                obj.ticket.save()
        record_audit("transaction.completed", obj)
        messages.success(request, "Transaction completed.")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return redirect("desk:transaction-list")


@protected("notification")
def notifications(request):
    if request.method == "POST":
        # Each row is scoped to the current user as well as their company.
        m.Notification.objects.filter(read_at__isnull=True).update(read_at=timezone.now())
        return redirect("desk:notifications")
    return render(
        request,
        "desk/notifications.html",
        {"notifications": m.Notification.objects.select_related("ticket")[:100]},
    )


@protected()
def search(request):
    query = request.GET.get("q", "").strip()[:200]
    results = []
    if query:
        if request.tenant.feature_flags.get("tickets", True):
            results += [
                {
                    "type": "Ticket",
                    "title": f"{obj.reference} · {obj.title}",
                    "url": reverse("desk:ticket-detail", args=[obj.uuid]),
                }
                for obj in m.Ticket.objects.filter(
                    Q(title__icontains=query) | Q(reference__icontains=query)
                )[:20]
            ]
        if request.tenant.feature_flags.get("policies", True):
            results += [
                {
                    "type": "Policy",
                    "title": obj.policy_number,
                    "url": reverse("desk:policy-detail", args=[obj.uuid]),
                }
                for obj in m.Policy.objects.filter(policy_number__icontains=query)[:20]
            ]
        if request.tenant.feature_flags.get("knowledge", True):
            results += [
                {
                    "type": "Knowledge",
                    "title": obj.title,
                    "url": reverse("desk:knowledge-detail", args=[obj.uuid]),
                }
                for obj in m.KnowledgeArticle.objects.filter(published=True).filter(
                    Q(title__icontains=query) | Q(body__icontains=query)
                )[:20]
            ]
    return render(request, "desk/search.html", {"query": query, "results": results})


@protected("knowledge", feature="knowledge")
def knowledge_detail(request, uuid):
    article = get_object_or_404(m.KnowledgeArticle.objects.filter(published=True), uuid=uuid)
    return render(request, "desk/article.html", {"article": article})


@protected("report", feature="analytics")
def reports(request):
    result, query_error = None, ""
    sql = request.POST.get("sql", "SELECT status, COUNT(*) AS total FROM tickets GROUP BY status")
    if request.method == "POST":
        try:
            result = execute_analytics(tenant=request.tenant, actor=request.user, sql=sql)
        except ValidationError as exc:
            query_error = "; ".join(exc.messages)
    jobs = m.Job.all_objects.filter(
        tenant=request.tenant, actor=request.user, job_type="REPORT_EXPORT"
    ).order_by("-created_at")[:10]
    return render(
        request,
        "desk/reports.html",
        {"sql": sql, "result": result, "query_error": query_error, "jobs": jobs},
    )


@protected("report", feature="analytics")
def export_tickets(request):
    response = HttpResponse(ticket_report(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="tickets.csv"'
    return response


@protected("report", feature="analytics")
@require_POST
def export_queue(request):
    queue_job(job_type="REPORT_EXPORT", actor=request.user)
    messages.success(request, "Report queued. The worker will recheck your access when it runs.")
    return redirect("desk:reports")


@protected("report", feature="analytics")
def export_download(request, uuid):
    job = get_object_or_404(
        m.Job.all_objects.filter(
            tenant=request.tenant, actor=request.user, state="COMPLETE", job_type="REPORT_EXPORT"
        ),
        uuid=uuid,
    )
    # Revalidation prevents a previous broader role or expired grant from exposing an old export.
    if job.payload.get("access_signature") != export_access_signature():
        raise PermissionDenied("Your access has changed. Generate a new report.")
    try:
        return FileResponse(job.result_file.open("rb"), as_attachment=True, filename="tickets.csv")
    except (FileNotFoundError, ValueError):
        raise Http404


def export_access_signature():
    ctx = current_context()
    return json.dumps(
        {"role": ctx.role, "grants": [(g.pk, g.scopes, g.resource_scope) for g in ctx.grants]},
        sort_keys=True,
    )


@protected("knowledge", feature="ai")
def ai_search(request):
    query = request.GET.get("q", "").strip()[:1000]
    results = (
        retrieve_knowledge(tenant=request.tenant, actor=request.user, query=query) if query else []
    )
    return render(request, "desk/ai_search.html", {"query": query, "results": results})


CONFIGS = {
    "projects": (m.Project, ["code", "name", "active"]),
    "products": (m.Product, ["project", "code", "name"]),
    "categories": (
        m.Category,
        ["product", "code", "name", "support_group", "sla", "workflow", "email_notifications"],
    ),
    "groups": (m.SupportGroup, ["code", "name"]),
    "group-members": (m.SupportGroupMember, ["group", "user", "active"]),
    "organization-types": (m.OrganizationType, ["code", "name"]),
    "slas": (m.SLA, ["name", "hours"]),
    "workflows": (m.Workflow, ["name", "stages", "rules"]),
    "mailboxes": (
        m.MailboxConfig,
        ["name", "email_address", "active", "graph_directory_id", "graph_client_id", "secret_env"],
    ),
    "authorities": (
        m.EmailAuthority,
        [
            "email_address",
            "organization",
            "policy",
            "permitted_transaction_types",
            "active",
            "valid_from",
            "valid_until",
        ],
    ),
    "ai-providers": (
        m.AIProviderConfig,
        [
            "name",
            "provider",
            "endpoint",
            "model",
            "token_env",
            "active",
            "supports_vision",
            "allow_sensitive_data",
            "timeout_seconds",
        ],
    ),
    "ai-prompts": (m.AIPrompt, ["purpose", "transaction_type", "prompt", "training_examples"]),
    "email-templates": (m.NotificationTemplate, ["name", "subject", "footer"]),
    "service-accounts": (m.ServiceAccount, ["name", "active", "scopes", "expires_at"]),
}


@protected("settings", feature="settings")
def settings(request):
    form = f.CompanySettingsForm(
        request.POST or None, request.FILES or None, instance=request.tenant
    )
    if request.method == "POST":
        require_capability("settings", "edit")
        if form.is_valid():
            form.save()
            from apps.tenancy.models import PlatformAuditEvent

            PlatformAuditEvent.objects.create(
                actor=request.user, tenant=request.tenant, action="company.settings_updated"
            )
            messages.success(request, "Company settings saved.")
            return redirect("desk:settings")
    return render(
        request,
        "desk/settings.html",
        {
            "form": form,
            "config_sections": [
                {"key": key, "title": key.replace("-", " ").title(), "count": model.objects.count()}
                for key, (model, _) in CONFIGS.items()
            ],
            "memberships": TenantMembership.objects.filter(tenant=request.tenant).select_related(
                "user"
            ),
            "invitation_form": f.InvitationForm(),
        },
    )


@protected("settings", feature="settings", action="edit")
def config_list(request, kind):
    if kind not in CONFIGS:
        raise Http404
    model, fields = CONFIGS[kind]
    objects = model.objects.all()
    return render(
        request,
        "desk/config_list.html",
        {
            "title": kind.replace("-", " ").title(),
            "kind": kind,
            "objects": objects,
            "fields": fields,
            "mailboxes": kind == "mailboxes",
            "inbound_emails": m.InboundEmail.objects.select_related("mailbox")[:50]
            if kind == "mailboxes"
            else [],
        },
    )


@protected("settings", feature="settings", action="edit")
def config_edit(request, kind, uuid=None):
    if kind not in CONFIGS:
        raise Http404
    model, fields = CONFIGS[kind]
    instance = get_object_or_404(model.objects, uuid=uuid) if uuid else None
    form_class = modelform_factory(model, form=f.ScopedModelForm, fields=fields)
    form = form_class(request.POST or None, tenant=request.tenant, instance=instance)
    token = None
    if kind == "service-accounts" and not instance:
        token, form.instance.token_hash = m.ServiceAccount.new_token()
        form.instance.created_by = request.user
    if request.method == "POST" and form.is_valid():
        obj = form.save()
        record_audit(f"configuration.{kind}.saved", obj)
        if token:
            return render(request, "desk/token.html", {"token": token, "account": obj})
        return redirect("desk:config-list", kind=kind)
    return render(
        request,
        "desk/form.html",
        {
            "form": form,
            "title": f"{'Edit' if instance else 'Add'} {model._meta.verbose_name}",
            "cancel_url": reverse("desk:config-list", args=[kind]),
        },
    )


@protected("settings", feature="settings", action="edit")
def membership_edit(request, pk):
    member = get_object_or_404(TenantMembership, pk=pk, tenant=request.tenant)
    form_class = modelform_factory(
        TenantMembership,
        fields=[
            "role",
            "active",
            "employee_number",
            "department",
            "job_title",
            "email_notifications",
            "browser_notifications",
            "valid_from",
            "valid_until",
        ],
    )
    form = form_class(request.POST or None, instance=member)
    if request.method == "POST" and form.is_valid():
        form.save()
        from apps.tenancy.models import PlatformAuditEvent

        PlatformAuditEvent.objects.create(
            actor=request.user,
            tenant=request.tenant,
            action="membership.updated",
            metadata={"membership_id": member.pk, "role": member.role, "active": member.active},
        )
        return redirect("desk:settings")
    return render(
        request,
        "desk/form.html",
        {
            "form": form,
            "title": f"Company access · {member.user}",
            "cancel_url": reverse("desk:settings"),
        },
    )


@protected("settings", feature="settings", action="edit")
@require_POST
def invite(request):
    form = f.InvitationForm(request.POST)
    if form.is_valid():
        invitation, token = create_invitation(actor=request.user, **form.cleaned_data)
        url = request.build_absolute_uri(reverse("tenancy:invitation-accept", args=[token]))
        return render(
            request, "desk/invitation_created.html", {"invitation": invitation, "invite_url": url}
        )
    messages.error(request, "Enter a valid email address and role.")
    return redirect("desk:settings")


@protected("settings", feature="mailbox", action="edit")
@require_POST
def mailbox_queue(request, uuid):
    mailbox = get_object_or_404(m.MailboxConfig.objects, uuid=uuid, active=True)
    queue_job(job_type="MAILBOX_SYNC", actor=request.user, payload={"mailbox_id": mailbox.pk})
    messages.success(request, "Mailbox sync queued.")
    return redirect("desk:config-list", kind="mailboxes")
