from .access import has_capability


def portal(request):
    tenant = getattr(request, "tenant", None)
    if not tenant:
        return {}
    from apps.desk.models import Notification

    role = getattr(request, "tenant_membership", None)
    modules = [
        ("ticket", "Tickets", "desk:ticket-list", "tickets"),
        ("policy", "Policies & members", "desk:policy-list", "policies"),
        ("organization", "Organizations", "desk:organization-list", "organizations"),
        ("task", "Tasks", "desk:task-list", "tasks"),
        ("transaction", "Endorsements & claims", "desk:transaction-list", "tpa"),
        ("document", "Documents", "desk:document-list", "documents"),
        ("knowledge", "Knowledge", "desk:knowledge-list", "knowledge"),
        ("report", "Reports", "desk:reports", "analytics"),
        ("audit", "Audit trail", "desk:audit-list", "audit"),
        ("settings", "Company settings", "desk:settings", "settings"),
    ]
    nav = [
        row for row in modules if tenant.feature_flags.get(row[3], True) and has_capability(row[0])
    ]
    return {
        "company": tenant,
        "companies": getattr(request, "allowed_companies", []),
        "portal_nav": nav,
        "company_role": role.get_role_display()
        if role
        else "Platform support"
        if request.support_session
        else "Limited access",
        "unread_notifications": Notification.objects.filter(read_at__isnull=True).count(),
        "can_create_ticket": has_capability("ticket", "edit"),
        "can_view_tickets": has_capability("ticket"),
        "can_view_policies": has_capability("policy"),
        "can_use_ai": has_capability("knowledge") and tenant.feature_flags.get("ai", True),
        "can_manage_company": has_capability("settings", "edit"),
    }
