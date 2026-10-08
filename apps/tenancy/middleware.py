from zoneinfo import ZoneInfo

from django.http import HttpResponseForbidden, HttpResponseRedirect, JsonResponse
from django.urls import reverse
from django.utils import timezone, translation

from .access import allowed_tenants, resolve_access
from .context import tenant_context
from .models import SupportSession, Tenant, TenantMembership


class TenantMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.tenant = None
        request.tenant_membership = None
        request.tenant_grants = ()
        request.support_session = None
        request.service_account = None
        request.allowed_companies = ()
        tenant, role, actor, grants, support = None, "", request.user, (), False
        authorization = request.headers.get("Authorization", "")
        if request.path.startswith("/api/") and authorization:
            from apps.desk.services import authenticate_service_account

            account = authenticate_service_account(authorization)
            if not account:
                return JsonResponse({"error": "Invalid or expired API credential."}, status=401)
            request.service_account = account
            tenant, actor = account.tenant, None
            # Header is a selector, never proof. Cross-company grants must explicitly authorize it.
            selected = request.headers.get("X-Tenant-ID")
            if selected and selected not in {str(tenant.pk), str(tenant.uuid)}:
                from uuid import UUID

                from .access import valid_grants

                try:
                    target = (
                        Tenant.objects.filter(uuid=UUID(selected)).first()
                        if len(selected) == 36
                        else Tenant.objects.filter(pk=selected).first()
                        if selected.isdigit()
                        else None
                    )
                except ValueError:
                    target = None
                grants = (
                    tuple(valid_grants(service_account=account).filter(target_tenant=target))
                    if target
                    else ()
                )
                if not grants:
                    return JsonResponse({"error": "Company access denied."}, status=403)
                tenant = target
                # Do not carry source account capabilities into another company.
                request.external_service_account = account
                request.service_account = None
        elif request.user.is_authenticated and request.user.is_active:
            companies = allowed_tenants(request.user)
            request.allowed_companies = list(companies)
            selected = request.session.get("active_tenant_id")
            if selected:
                tenant = companies.filter(pk=selected).first()
            elif companies.exists():
                default = (
                    TenantMembership.objects.valid()
                    .filter(user=request.user, is_default=True)
                    .first()
                )
                tenant = default.tenant if default else companies.first()
                request.session["active_tenant_id"] = tenant.pk
            if tenant:
                membership, grants = resolve_access(request.user, tenant)
                request.tenant_membership = membership
                role = membership.role if membership else ""
            support_uuid = request.session.get("support_session_uuid")
            if support_uuid and request.user.platform_operator:
                active = (
                    SupportSession.objects.filter(
                        uuid=support_uuid,
                        user=request.user,
                        ended_at__isnull=True,
                        expires_at__gt=timezone.now(),
                        tenant__active=True,
                        tenant__status="ACTIVE",
                    )
                    .select_related("tenant")
                    .first()
                )
                if active and selected == active.tenant_id:
                    request.support_session, tenant, role, support = (
                        active,
                        active.tenant,
                        "ADMIN",
                        True,
                    )
                elif not active:
                    request.session.pop("support_session_uuid", None)
        exempt = request.path.startswith(("/login/", "/logout/", "/companies/", "/static/"))
        platform_admin = (
            request.path.startswith("/admin/")
            and request.user.is_authenticated
            and request.user.platform_operator
        )
        if not tenant and not exempt and not platform_admin:
            if request.path.startswith("/api/"):
                return JsonResponse(
                    {"error": "An authorized company context is required."},
                    status=403 if request.user.is_authenticated else 401,
                )
            if request.user.is_authenticated:
                return HttpResponseRedirect(reverse("tenancy:select"))
            return HttpResponseRedirect(reverse("login") + "?next=" + request.path)
        if not tenant:
            response = self.get_response(request)
            response["Cache-Control"] = "private, no-store"
            return response
        request.tenant, request.tenant_grants = tenant, grants
        # A stale form or HTMX fragment is rejected after switching company.
        expected = request.headers.get("X-Company-Context") or request.POST.get("_company_context")
        unsafe_portal = request.method in {
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        } and not request.path.startswith(("/api/", "/admin/", "/logout/", "/companies/"))
        if (expected and expected != str(tenant.uuid)) or (unsafe_portal and not expected):
            return HttpResponseForbidden("Company changed. Reload this page before continuing.")
        with tenant_context(
            tenant,
            actor=actor,
            role=role,
            grants=grants,
            support=support,
            service_account=request.service_account,
        ):
            with (
                timezone.override(ZoneInfo(tenant.default_timezone)),
                translation.override(tenant.default_language),
            ):
                response = self.get_response(request)
                # Render template responses before leaving context. Streaming data must be materialized by its service.
                if hasattr(response, "render") and not response.is_rendered:
                    response.render()
        response["Cache-Control"] = "private, no-store"
        response["Vary"] = "Cookie, Authorization, X-Company-Context, HX-Request"
        response["X-Company-Context"] = str(tenant.uuid)
        return response
