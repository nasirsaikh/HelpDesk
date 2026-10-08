import hashlib
import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from .access import require_capability
from .context import current_context, tenant_context
from .models import PlatformAuditEvent, Tenant, TenantMembership


@transaction.atomic
def onboard_company(*, code, name, admin_email, operator=None):
    """Trusted platform/CLI entry point. Repeating it does not duplicate setup."""
    from apps.desk import models as m

    if operator and not operator.platform_operator:
        raise PermissionDenied("Platform administration is required.")
    tenant, created = Tenant.objects.get_or_create(
        code=code.upper(), defaults={"name": name, "slug": slugify(code)}
    )
    if not tenant.available:
        raise ValidationError("Reactivate this company before onboarding it.")
    email = admin_email.strip().lower()
    user = get_user_model().objects.filter(email__iexact=email).first()
    if not user:
        username = (
            email
            if len(email) <= 150
            else email[:130] + hashlib.sha256(email.encode()).hexdigest()[:12]
        )
        user = get_user_model().objects.create_user(username=username, email=email)
    if not user.is_active:
        raise ValidationError(
            "Reactivate the global identity before assigning administrator membership."
        )
    with tenant_context(tenant, actor=user, system=True):
        membership, _ = TenantMembership.objects.get_or_create(
            tenant=tenant,
            user=user,
            defaults={
                "role": "ADMIN",
                "is_default": not user.tenant_memberships.filter(is_default=True).exists(),
                "created_by": operator,
            },
        )
        if membership.role != "ADMIN":
            raise ValidationError(
                "Existing membership is not an administrator; update it deliberately in platform administration."
            )
        types = {}
        for type_code, title in [
            ("INSURER", "Insurance company"),
            ("INDIVIDUAL", "Individual"),
            ("CORPORATE", "Corporate"),
            ("BROKER", "Broker"),
            ("AGENT", "Agent"),
            ("BRANCH", "Branch"),
            ("TPA", "Third party administrator"),
            ("OTHER", "Other"),
        ]:
            types[type_code], _ = m.OrganizationType.objects.get_or_create(
                code=type_code, defaults={"name": title}
            )
        root, _ = m.Organization.objects.get_or_create(
            code="ROOT", defaults={"name": name, "organization_type": types["INSURER"]}
        )
        group, _ = m.SupportGroup.objects.get_or_create(
            code="SUPPORT", defaults={"name": "Support operations"}
        )
        m.SupportGroupMember.objects.get_or_create(group=group, user=user)
        sla, _ = m.SLA.objects.get_or_create(name="Standard", defaults={"hours": 40})
        workflow, _ = m.Workflow.objects.get_or_create(
            name="Service request", defaults={"stages": ["INTAKE", "VALIDATION", "COMPLETE"]}
        )
        medical, _ = m.Workflow.objects.get_or_create(
            name="Medical endorsement",
            defaults={
                "rules": {"backdating_days": 30, "tpa_required": True, "card_required": True}
            },
        )
        for prefix, title in [
            ("GLIS", "HelpDesk"),
            ("POL", "Policy enrollment"),
            ("ADD", "Member addition"),
            ("DEL", "Member deletion"),
            ("CLM", "Claims"),
        ]:
            project, _ = m.Project.objects.get_or_create(code=prefix, defaults={"name": title})
            product, _ = m.Product.objects.get_or_create(
                code=prefix,
                defaults={
                    "name": "General services" if prefix == "GLIS" else title,
                    "project": project,
                },
            )
            m.Category.objects.get_or_create(
                product=product,
                code="GENERAL",
                defaults={
                    "name": title,
                    "sla": sla,
                    "workflow": workflow if prefix == "GLIS" else medical,
                    "support_group": group,
                },
            )
        m.NotificationTemplate.objects.get_or_create(
            name="ticket-update", defaults={"footer": f"{name} · HelpDesk"}
        )
    PlatformAuditEvent.objects.create(
        actor=operator,
        tenant=tenant,
        action="company.onboarded" if created else "company.onboarding_repeated",
    )
    return tenant, user, root


def create_invitation(*, email, role, actor):
    from apps.desk.models import Invitation
    from apps.desk.services import record_audit

    require_capability("settings", "edit")
    ctx = current_context()
    token = secrets.token_urlsafe(40)
    invitation = Invitation.objects.create(
        tenant=ctx.tenant,
        email=email.lower(),
        role=role,
        invited_by=actor,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        expires_at=timezone.now() + timedelta(days=7),
    )
    record_audit("user.invited", invitation, metadata={"role": role})
    return invitation, token


@transaction.atomic
def accept_invitation(*, token, user):
    from apps.desk.models import Invitation
    from apps.desk.services import record_audit

    digest = hashlib.sha256(token.encode()).hexdigest()
    # The unguessable invitation token is deliberately checked before company context is established.
    invitation = (
        Invitation.all_objects.select_for_update()
        .select_related("tenant")
        .filter(
            token_hash=digest,
            email__iexact=user.email,
            accepted_at__isnull=True,
            expires_at__gt=timezone.now(),
            tenant__active=True,
            tenant__status="ACTIVE",
        )
        .first()
    )
    if not invitation or not user.is_active:
        raise PermissionDenied(
            "This invitation is invalid, expired, or addressed to another account."
        )
    with tenant_context(invitation.tenant, actor=user, system=True):
        membership, created = TenantMembership.objects.get_or_create(
            tenant=invitation.tenant,
            user=user,
            defaults={"role": invitation.role, "created_by": invitation.invited_by},
        )
        if not created and not membership.active:
            raise PermissionDenied("Ask a company administrator to reactivate your membership.")
        # Acceptance never silently elevates an existing role.
        invitation.accepted_at = timezone.now()
        invitation.save()
        record_audit("invitation.accepted", invitation)
    return membership
