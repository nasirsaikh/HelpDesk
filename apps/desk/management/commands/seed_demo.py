import os
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.desk import models as m
from apps.desk.services import create_ticket
from apps.tenancy.context import tenant_context
from apps.tenancy.models import TenantMembership
from apps.tenancy.services import onboard_company


class Command(BaseCommand):
    help = "Create two isolated demonstration companies. Explicit development-only opt-in required."

    def add_arguments(self, parser):
        parser.add_argument("--allow-demo", action="store_true")

    @transaction.atomic
    def handle(self, **options):
        if not settings.DEBUG or not options["allow_demo"]:
            raise CommandError("Demo data requires DEBUG=1 and --allow-demo.")
        password = os.environ.get("DEMO_PASSWORD") or secrets.token_urlsafe(16)
        for code, name, email in [
            ("TAKAFUL_OMAN", "Takaful Oman", "demo.admin@takaful.example"),
            ("ABC_INSURANCE", "ABC Insurance", "demo.admin@abc.example"),
        ]:
            tenant, admin, root = onboard_company(code=code, name=name, admin_email=email)
            admin.first_name = "Nasir" if code == "TAKAFUL_OMAN" else "Sarah"
            admin.last_name = "Ali" if code == "TAKAFUL_OMAN" else "Ahmed"
            admin.set_password(password)
            admin.save()
            with tenant_context(tenant, actor=admin, system=True):
                category = m.Category.objects.get(product__code="GLIS")
                policy, _ = m.Policy.objects.get_or_create(
                    policy_number="P/100/2026/001",
                    defaults={
                        "organization": root,
                        "product": m.Product.objects.get(code="POL"),
                        "start_date": timezone.localdate(),
                        "end_date": timezone.localdate() + timedelta(days=365),
                        "status": "ACTIVE",
                    },
                )
                plan, _ = m.BenefitPlan.objects.get_or_create(
                    policy=policy,
                    code="GOLD",
                    defaults={"name": "Gold medical", "annual_premium": 180, "sum_assured": 5000},
                )
                for index, full_name in enumerate(
                    [
                        "Ahmed Khalid",
                        "Maryam Hassan",
                        "Omar Saeed",
                        "Fatima Noor",
                        "Khalid Ibrahim",
                        "Sara Mahmood",
                    ]
                ):
                    m.Member.objects.get_or_create(
                        policy=policy,
                        member_id=str(1001 + index),
                        defaults={
                            "plan": plan,
                            "full_name": full_name,
                            "date_of_birth": "1990-06-12",
                            "gender": "MALE" if index % 2 == 0 else "FEMALE",
                            "active": index < 5,
                            "card_number": f"CARD-{index + 1:04d}",
                        },
                    )
                article, _ = m.KnowledgeArticle.objects.get_or_create(
                    title="How to raise a medical card request",
                    defaults={
                        "body": "Create a request under your medical project, link the policy and include the member ID. Your support team will validate the request and coordinate card issuance.",
                        "published": True,
                        "category": "Medical operations",
                    },
                )
                m.VectorDocument.objects.get_or_create(
                    article=article,
                    defaults={"content": article.body, "namespace": f"tenant_{tenant.uuid.hex}"},
                )
            if not m.Ticket.all_objects.filter(tenant=tenant).exists():
                for index, title in enumerate(
                    [
                        "Medical card replacement",
                        "New employee enrollment",
                        "Policy document correction",
                        "Update contact details",
                        "Premium clarification",
                        "Dependent card issue",
                    ]
                ):
                    ticket = create_ticket(
                        tenant=tenant,
                        actor=admin,
                        data={
                            "title": title,
                            "description": "Please review this request and coordinate with the relevant team. Supporting information is available from the requester.",
                            "project": category.product.project,
                            "category": category,
                            "priority": "HIGH" if index == 0 else "NORMAL",
                            "assigned_to": admin,
                        },
                    )
                    with tenant_context(tenant, actor=admin, system=True):
                        if index in {0, 2}:
                            ticket.status, ticket.workflow_stage = "IN_PROGRESS", "VALIDATION"
                        if index == 4:
                            ticket.status, ticket.workflow_stage, ticket.completed_at = (
                                "COMPLETE",
                                "COMPLETE",
                                timezone.now(),
                            )
                        if index == 0:
                            ticket.due_at = timezone.now() - timedelta(hours=2)
                        ticket.save()
            self.stdout.write(f"Company {name}: username {admin.username}")
        first = TenantMembership.objects.get(
            tenant__code="TAKAFUL_OMAN", user__email="demo.admin@takaful.example"
        ).user
        from apps.tenancy.models import Tenant

        TenantMembership.objects.get_or_create(
            tenant=Tenant.objects.get(code="ABC_INSURANCE"),
            user=first,
            defaults={"role": "AUDITOR"},
        )
        self.stdout.write(self.style.SUCCESS(f"Demo password (development only): {password}"))
