from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .access import allowed_tenants
from .models import PlatformAuditEvent, SupportSession, Tenant
from .services import accept_invitation


@login_required
def select(request):
    return render(
        request,
        "tenancy/select.html",
        {
            "available_companies": allowed_tenants(request.user),
            "platform_companies": Tenant.objects.all() if request.user.platform_operator else [],
        },
    )


def reset_company_session(request, tenant):
    # Preserve authentication and global language only. Invalidate every company-specific wizard/filter state.
    for key in list(request.session.keys()):
        if not key.startswith("_auth_") and key not in {"django_language"}:
            request.session.pop(key, None)
    request.session.cycle_key()
    request.session["active_tenant_id"] = tenant.pk


@login_required
@require_POST
@transaction.atomic
def switch(request):
    tenant = get_object_or_404(allowed_tenants(request.user), uuid=company_uuid(request))
    support_id = request.session.get("support_session_uuid")
    if support_id:
        SupportSession.objects.filter(
            uuid=support_id, user=request.user, ended_at__isnull=True
        ).update(ended_at=timezone.now())
    reset_company_session(request, tenant)
    PlatformAuditEvent.objects.create(actor=request.user, tenant=tenant, action="company.switched")
    response = redirect("desk:dashboard")
    if request.headers.get("HX-Request"):
        response.status_code = 200
        response["HX-Redirect"] = "/"
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
@require_POST
@transaction.atomic
def support_start(request):
    if not request.user.platform_operator:
        raise PermissionDenied("Platform operator access is required.")
    tenant = get_object_or_404(Tenant, uuid=company_uuid(request), active=True, status="ACTIVE")
    reason = request.POST.get("reason", "").strip()
    if len(reason) < 10:
        messages.error(request, "Enter a support reason of at least 10 characters.")
        return redirect("tenancy:select")
    old = request.session.get("support_session_uuid")
    if old:
        SupportSession.objects.filter(uuid=old, user=request.user, ended_at__isnull=True).update(
            ended_at=timezone.now()
        )
    session = SupportSession.objects.create(
        tenant=tenant,
        user=request.user,
        reason=reason,
        reference=request.POST.get("reference", "")[:100],
        expires_at=timezone.now() + timedelta(minutes=30),
    )
    reset_company_session(request, tenant)
    request.session["support_session_uuid"] = str(session.uuid)
    PlatformAuditEvent.objects.create(
        actor=request.user,
        tenant=tenant,
        action="support.started",
        reason=reason,
        metadata={
            "support_session": str(session.uuid),
            "expires_at": session.expires_at.isoformat(),
        },
    )
    return redirect("desk:dashboard")


@login_required
@require_POST
def support_end(request):
    session = SupportSession.objects.filter(
        uuid=request.session.get("support_session_uuid"), user=request.user, ended_at__isnull=True
    ).first()
    if session:
        session.ended_at = timezone.now()
        session.save(update_fields=["ended_at"])
        PlatformAuditEvent.objects.create(
            actor=request.user, tenant=session.tenant, action="support.ended", reason=session.reason
        )
    request.session.pop("support_session_uuid", None)
    request.session.pop("active_tenant_id", None)
    return redirect("tenancy:select")


def invitation_accept(request, token):
    import hashlib

    from django.contrib.auth import get_user_model, login
    from django.contrib.auth.forms import UserCreationForm
    from django.forms import modelform_factory

    from apps.desk.models import Invitation

    invitation = get_object_or_404(
        Invitation.all_objects.filter(
            accepted_at__isnull=True,
            expires_at__gt=timezone.now(),
            tenant__active=True,
            tenant__status="ACTIVE",
        ),
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
    )
    registration_form = None
    if not request.user.is_authenticated:
        if get_user_model().objects.filter(email__iexact=invitation.email).exists():
            from django.contrib.auth.views import redirect_to_login

            return redirect_to_login(request.get_full_path())
        form_class = modelform_factory(
            get_user_model(), form=UserCreationForm, fields=["username", "first_name", "last_name"]
        )
        registration_form = form_class(request.POST or None)
        if request.method == "POST" and registration_form.is_valid():
            with transaction.atomic():
                user = registration_form.save(commit=False)
                user.email = invitation.email
                user.save()
                accept_invitation(token=token, user=user)
            login(request, user)
            reset_company_session(request, invitation.tenant)
            return redirect("desk:dashboard")
        return render(request, "tenancy/invitation.html", {"registration_form": registration_form})
    if request.method == "POST":
        membership = accept_invitation(token=token, user=request.user)
        reset_company_session(request, membership.tenant)
        messages.success(request, "Company invitation accepted.")
        return redirect("desk:dashboard")
    return render(request, "tenancy/invitation.html")


def company_uuid(request):
    from uuid import UUID

    try:
        return UUID(request.POST.get("company", ""))
    except (ValueError, TypeError, AttributeError):
        raise Http404


@login_required
def onboard(request):
    from django import forms

    from .services import onboard_company

    if not request.user.platform_operator:
        raise PermissionDenied

    class OnboardForm(forms.Form):
        code = forms.RegexField(regex=r"^[A-Za-z][A-Za-z0-9_]{1,39}$", max_length=40)
        name = forms.CharField(max_length=160)
        admin_email = forms.EmailField()

    form = OnboardForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            tenant, user, _ = onboard_company(operator=request.user, **form.cleaned_data)
            messages.success(
                request,
                f"{tenant.name} created. Administrator: {user.username}. Set a password with manage.py changepassword if this is a new account.",
            )
            return redirect("tenancy:select")
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
    return render(
        request,
        "desk/form.html",
        {
            "form": form,
            "title": "Create company",
            "subtitle": "Create branding, organization types, projects, workflows and administrator membership.",
            "cancel_url": "/companies/",
        },
    )


@login_required
def branding(request, kind):
    from django.http import FileResponse

    if kind not in {"logo", "favicon"} or not request.tenant:
        raise Http404
    file = getattr(request.tenant, kind)
    if not file or not file.name.startswith(f"tenants/{request.tenant.uuid}/"):
        raise Http404
    try:
        return FileResponse(file.open("rb"))
    except FileNotFoundError:
        raise Http404
