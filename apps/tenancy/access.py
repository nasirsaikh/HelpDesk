"""Capabilities, company boundaries and object restrictions are evaluated together."""

from django.db.models import Q
from django.utils import timezone

from .context import current_context
from .models import Tenant, TenantAccessGrant, TenantMembership

GRANT_SCOPES = {
    "TENANT_VIEW": [("*", "view")],
    "TENANT_ADMIN": [("*", "view"), ("*", "edit")],
    "TICKET_VIEW": [("ticket", "view")],
    "TICKET_EDIT": [("ticket", "view"), ("ticket", "edit")],
    "POLICY_VIEW": [("policy", "view")],
    "POLICY_ADMIN": [("policy", "view"), ("policy", "edit")],
    "REPORT_VIEW": [("report", "view")],
    "AUDIT_VIEW": [("audit", "view")],
    "SUPPORT": [("*", "view"), ("*", "edit")],
    "APPROVAL": [("ticket", "view"), ("approval", "edit")],
    "TPA_PROCESSING": [("policy", "view"), ("transaction", "view"), ("transaction", "edit")],
}
OPERATIONAL = {
    "ticket",
    "task",
    "approval",
    "policy",
    "transaction",
    "organization",
    "document",
    "knowledge",
    "report",
    "notification",
}
RESOURCE_FEATURES = {
    "ticket": "tickets",
    "approval": "tickets",
    "task": "tasks",
    "policy": "policies",
    "transaction": "tpa",
    "organization": "organizations",
    "document": "documents",
    "knowledge": "knowledge",
    "report": "analytics",
    "audit": "audit",
    "settings": "settings",
    "mailbox": "mailbox",
}


def valid_grants(user=None, service_account=None):
    now = timezone.now()
    qs = TenantAccessGrant.objects.filter(
        active=True,
        valid_from__lte=now,
        target_tenant__active=True,
        target_tenant__status="ACTIVE",
        source_tenant__active=True,
        source_tenant__status="ACTIVE",
    ).filter(Q(valid_until__isnull=True) | Q(valid_until__gt=now))
    if service_account:
        return qs.filter(service_account=service_account)
    if not user or not user.is_active:
        return qs.none()
    from apps.desk.models import SupportGroupMember

    sources = TenantMembership.objects.valid().filter(user=user).values_list("tenant_id", flat=True)
    groups = SupportGroupMember.all_objects.filter(
        user=user, active=True, tenant_id__in=sources
    ).values_list("group_id", flat=True)
    return qs.filter(source_tenant_id__in=sources).filter(Q(user=user) | Q(group_id__in=groups))


def allowed_tenants(user):
    if not user.is_authenticated or not user.is_active:
        return Tenant.objects.none()
    membership_ids = (
        TenantMembership.objects.valid().filter(user=user).values_list("tenant_id", flat=True)
    )
    grant_ids = valid_grants(user).values_list("target_tenant_id", flat=True)
    return (
        Tenant.objects.filter(active=True, status="ACTIVE")
        .filter(Q(pk__in=membership_ids) | Q(pk__in=grant_ids))
        .distinct()
    )


def resolve_access(user, tenant):
    if not user.is_authenticated or not user.is_active or not tenant.available:
        return None, ()
    membership = TenantMembership.objects.valid().filter(user=user, tenant=tenant).first()
    grants = tuple(valid_grants(user).filter(target_tenant=tenant))
    return membership, grants


def grant_allows(grant, resource, action):
    if resource == "task" or (resource == "approval" and action == "view"):
        resource = "ticket"
    return any(
        (resource, action) in GRANT_SCOPES.get(scope, ())
        or ("*", action) in GRANT_SCOPES.get(scope, ())
        for scope in grant.scopes
    )


def role_allows(ctx, resource, action):
    if ctx.system or ctx.support:
        return True
    if ctx.service_account:
        return any(
            scope in {f"{resource}.{action}", f"*.{action}"} for scope in ctx.service_account.scopes
        )
    if resource == "internal":
        return False
    if ctx.role == "ADMIN":
        return True
    if ctx.role == "AUDITOR":
        return action == "view" and resource != "settings"
    if ctx.role == "MANAGER":
        return resource in OPERATIONAL or (action == "view" and resource in {"settings", "audit"})
    if ctx.role == "AGENT":
        return resource in OPERATIONAL - {"report"} or (action == "view" and resource == "report")
    if ctx.role == "USER":
        return resource in {"ticket", "notification"} or (
            action == "view" and resource in {"knowledge", "document"}
        )
    return False


def has_capability(resource, action="view", ctx=None):
    ctx = ctx or current_context()
    if (
        ctx
        and not ctx.system
        and not ctx.tenant.feature_flags.get(RESOURCE_FEATURES.get(resource, resource), True)
    ):
        return False
    return bool(
        ctx
        and ctx.tenant.available
        and (
            role_allows(ctx, resource, action)
            or any(grant_allows(g, resource, action) for g in ctx.grants)
        )
    )


def restriction_q(model, grant):
    filters = {}
    for key, value in grant.resource_scope.items():
        path = getattr(model, "grant_paths", {}).get(key)
        if not path:
            return Q(pk__in=[])
        filters[path] = value
    return Q(**filters)


def authorization_q(model, ctx, action="view"):
    resource = getattr(model, "permission_resource", "settings")
    if role_allows(ctx, resource, action):
        result = Q()
    else:
        result = Q(pk__in=[])
        for grant in ctx.grants:
            if grant_allows(grant, resource, action):
                result |= restriction_q(model, grant)
    if ctx.role == "USER" and not ctx.system and not ctx.support:
        paths = getattr(model, "requester_paths", ())
        if paths:
            own = Q(pk__in=[])
            for path in paths:
                own |= Q(**{path: ctx.actor.pk})
            result &= own
        elif resource == "ticket":
            result &= Q(pk__in=[])
    if resource == "notification" and not ctx.system:
        result &= Q(user=ctx.actor) if ctx.actor else Q(pk__in=[])
    return result


def scoped_queryset(queryset, ctx, action="view"):
    if not ctx or not ctx.tenant.available:
        return queryset.none()
    if not ctx.system and not ctx.tenant.feature_flags.get(
        RESOURCE_FEATURES.get(
            getattr(queryset.model, "permission_resource", "settings"), "settings"
        ),
        True,
    ):
        return queryset.none()
    queryset = queryset.filter(tenant_id=ctx.tenant_id).filter(
        authorization_q(queryset.model, ctx, action)
    )
    if not ctx.system:
        name = queryset.model._meta.model_name
        if name == "transaction" and not ctx.tenant.feature_flags.get("claims", True):
            queryset = queryset.exclude(transaction_type="CLAIM")
        if name == "ticket":
            for kind, flag in [("CLAIM", "claims"), ("POLICY", "policies"), ("ENDORSEMENT", "tpa")]:
                if not ctx.tenant.feature_flags.get(flag, True):
                    queryset = queryset.exclude(request_type=kind)
    if ctx.role == "USER" and not ctx.system:
        if queryset.model._meta.model_name == "comment":
            queryset = queryset.filter(internal=False)
        if queryset.model._meta.model_name in {"knowledgearticle", "vectordocument"}:
            queryset = queryset.filter(
                **(
                    {"published": True}
                    if queryset.model._meta.model_name == "knowledgearticle"
                    else {"article__published": True}
                )
            )
    return queryset


def require_capability(resource, action="view"):
    from django.core.exceptions import PermissionDenied

    if not has_capability(resource, action):
        raise PermissionDenied("Your company role does not allow this operation.")


def object_matches_grant(obj, grant):
    for key, expected in grant.resource_scope.items():
        path = getattr(type(obj), "grant_paths", {}).get(key)
        if not path:
            return False
        value = obj
        try:
            for part in path.split("__"):
                value = getattr(value, part)
        except (AttributeError, ObjectDoesNotExist):
            return False
        if value != expected:
            return False
    return True


from django.core.exceptions import ObjectDoesNotExist  # noqa: E402
