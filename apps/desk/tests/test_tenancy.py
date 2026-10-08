import io
import json
import shutil
import tempfile
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.desk import models as m
from apps.desk.ai import execute_analytics, resolve_prompt, retrieve_knowledge
from apps.desk.forms import PolicyForm
from apps.desk.integrations import process_inbound, resolve_email_authority, sync_mailbox
from apps.desk.jobs import queue_job, run_job, send_outbox
from apps.desk.services import advance_ticket, create_ticket, decide_approval
from apps.tenancy.access import allowed_tenants, resolve_access
from apps.tenancy.cache import tenant_cache_key
from apps.tenancy.context import current_context, tenant_context
from apps.tenancy.models import (
    PlatformAuditEvent,
    SupportSession,
    Tenant,
    TenantAccessGrant,
    TenantMembership,
)
from apps.tenancy.services import accept_invitation, create_invitation, onboard_company


class TenantIsolationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media = tempfile.mkdtemp(prefix="helpdesk-isolation-")
        cls.media_settings = override_settings(MEDIA_ROOT=cls.media)
        cls.media_settings.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.media_settings.disable()
        shutil.rmtree(cls.media, ignore_errors=True)

    @classmethod
    def setUpTestData(cls):
        cls.a, cls.alice, cls.org_a = onboard_company(
            code="COMPANY_A", name="Company A", admin_email="alice@example.com"
        )
        cls.b, cls.bob, cls.org_b = onboard_company(
            code="COMPANY_B", name="Company B", admin_email="bob@example.com"
        )
        cls.platform = get_user_model().objects.create_superuser(
            "platform", "platform@example.com", "StrongSecret!234"
        )
        cls.dual = get_user_model().objects.create_user(
            "dual", "dual@example.com", "StrongSecret!234"
        )
        cls.requester = get_user_model().objects.create_user(
            "requester", "requester@example.com", "StrongSecret!234"
        )
        cls.staff = get_user_model().objects.create_user(
            "staff", "staff@example.com", "StrongSecret!234", is_staff=True
        )
        TenantMembership.objects.create(tenant=cls.a, user=cls.dual, role="ADMIN", is_default=True)
        TenantMembership.objects.create(tenant=cls.b, user=cls.dual, role="AUDITOR")
        TenantMembership.objects.create(tenant=cls.a, user=cls.requester, role="USER")
        cls.data = {}
        for tenant, actor, organization in [
            (cls.a, cls.alice, cls.org_a),
            (cls.b, cls.bob, cls.org_b),
        ]:
            with tenant_context(tenant, actor=actor, system=True):
                category = m.Category.objects.get(product__code="GLIS")
                policy = m.Policy.objects.create(
                    policy_number="P/100/2026/001",
                    organization=organization,
                    product=m.Product.objects.get(code="POL"),
                    start_date=timezone.localdate(),
                    end_date=timezone.localdate() + timedelta(days=365),
                    status="ACTIVE",
                )
                plan = m.BenefitPlan.objects.create(
                    policy=policy, code="GOLD", name="Gold", annual_premium=100
                )
                member = m.Member.objects.create(
                    policy=policy,
                    plan=plan,
                    member_id="1001",
                    full_name=f"{tenant.name} private member",
                    date_of_birth="1990-01-01",
                    gender="MALE",
                )
                ticket = m.Ticket.objects.create(
                    reference="GLIS-2026-000001",
                    title="Card issue",
                    description=f"{tenant.name} PRIVATE-{tenant.code}-789",
                    project=category.product.project,
                    category=category,
                    requester=actor,
                    workflow_stage="INTAKE",
                )
                attachment = m.Attachment.objects.create(
                    ticket=ticket,
                    uploaded_by=actor,
                    file=SimpleUploadedFile(
                        "passport.pdf", tenant.code.encode(), content_type="application/pdf"
                    ),
                    original_name="passport.pdf",
                    size=9,
                )
                document = m.Document.objects.create(
                    title="Private passport",
                    policy=policy,
                    uploaded_by=actor,
                    file=SimpleUploadedFile("passport.pdf", tenant.code.encode()),
                    original_name="passport.pdf",
                )
                article = m.KnowledgeArticle.objects.create(
                    title="Card issue", body=f"SECRET-{tenant.code}-789", published=True
                )
                vector = m.VectorDocument.objects.create(
                    article=article,
                    content=article.body,
                    embedding=[1, 0],
                    namespace=f"tenant_{tenant.uuid.hex}",
                )
                mailbox = m.MailboxConfig.objects.create(
                    name="Endorsements",
                    email_address=f"intake@{tenant.code.lower().replace('_', '-')}.example",
                    secret_env=f"TENANT_{tenant.uuid.hex.upper()}_GRAPH_SECRET",
                )
                email = m.InboundEmail.objects.create(
                    mailbox=mailbox,
                    message_id="SAME-MESSAGE",
                    sender="authorized@example.com",
                    subject="Addition P/100/2026/001",
                    body="Add member",
                )
                authority = m.EmailAuthority.objects.create(
                    email_address="authorized@example.com",
                    organization=organization,
                    policy=policy,
                    permitted_transaction_types=["MEMBER_ADD"],
                )
                prompt = m.AIPrompt.objects.create(
                    purpose="EXTRACTION", prompt=f"TRAINING-{tenant.code}-PRIVATE"
                )
                audit = m.AuditEvent.objects.create(
                    actor=actor,
                    action="test.created",
                    resource_type="Ticket",
                    resource_uuid=str(ticket.uuid),
                )
                note = m.Notification.objects.create(
                    user=actor, ticket=ticket, message=f"{tenant.code} PRIVATE notification"
                )
                cls.data[tenant.code] = {
                    "category": category,
                    "policy": policy,
                    "plan": plan,
                    "member": member,
                    "ticket": ticket,
                    "attachment": attachment,
                    "document": document,
                    "article": article,
                    "vector": vector,
                    "mailbox": mailbox,
                    "email": email,
                    "authority": authority,
                    "prompt": prompt,
                    "audit": audit,
                    "notification": note,
                }

    def setUp(self):
        cache.clear()
        self.da, self.db = self.data[self.a.code], self.data[self.b.code]
        self.client.force_login(self.alice)
        self.set_company(self.a)

    def set_company(self, tenant, client=None):
        client = client or self.client
        session = client.session
        session["active_tenant_id"] = tenant.pk
        session.save()

    def post(self, url, data=None, tenant=None, **kwargs):
        payload = {"_company_context": str((tenant or self.a).uuid), **(data or {})}
        return self.client.post(url, payload, **kwargs)

    def grant(self, scopes, restrictions=None, **kwargs):
        return TenantAccessGrant.objects.create(
            source_tenant=self.a,
            target_tenant=self.b,
            user=kwargs.pop("user", self.alice),
            scopes=scopes,
            resource_scope=restrictions or {},
            reason="Temporary external policy audit",
            created_by=self.platform,
            approved_by=self.platform,
            **kwargs,
        )

    def scope(self, tenant=None, actor=None):
        tenant, actor = tenant or self.a, actor or self.alice
        membership, grants = resolve_access(actor, tenant)
        return tenant_context(
            tenant, actor=actor, role=membership.role if membership else "", grants=grants
        )

    def test_unscoped_default_manager_denies_read_and_write(self):
        self.assertEqual(m.Ticket.objects.count(), 0)
        self.assertFalse(m.Policy.objects.for_tenant(self.a).exists())
        with self.assertRaises(PermissionDenied):
            m.OrganizationType.objects.create(tenant=self.a, code="DENIED", name="Denied")

    def test_every_business_model_has_company_ownership(self):
        from django.apps import apps

        for model in apps.get_app_config("desk").get_models():
            if model != m.PlatformArticle:
                self.assertIsNotNone(model._meta.get_field("tenant"), model.__name__)
                self.assertEqual(model._default_manager.name, "objects")

    def test_context_restores_and_does_not_cross_threads(self):
        with self.scope():
            self.assertEqual(current_context().tenant_id, self.a.pk)
            with self.scope(self.b, self.bob):
                self.assertEqual(current_context().tenant_id, self.b.pk)
            self.assertEqual(current_context().tenant_id, self.a.pk)
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertIsNone(pool.submit(current_context).result())
        self.assertIsNone(current_context())

    def test_equal_references_allowed_in_separate_companies(self):
        self.assertEqual(self.da["policy"].policy_number, self.db["policy"].policy_number)
        self.assertEqual(self.da["ticket"].reference, self.db["ticket"].reference)
        self.assertEqual(self.da["member"].member_id, self.db["member"].member_id)
        with self.scope():
            self.assertEqual(m.Policy.objects.count(), 1)
            self.assertEqual(
                m.Policy.objects.get(policy_number="P/100/2026/001").pk, self.da["policy"].pk
            )

    def test_same_company_duplicate_number_rejected(self):
        with self.scope(), self.assertRaises(ValidationError):
            m.Policy.objects.create(
                policy_number=self.da["policy"].policy_number,
                organization=self.org_a,
                product=self.da["policy"].product,
                start_date=timezone.localdate(),
                end_date=timezone.localdate(),
            )

    def test_list_is_scoped(self):
        response = self.client.get("/tickets/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([obj.pk for obj in response.context["page"]], [self.da["ticket"].pk])

    def test_foreign_ticket_uuid_returns_404(self):
        self.assertEqual(
            self.client.get(
                reverse("desk:ticket-detail", args=[self.db["ticket"].uuid])
            ).status_code,
            404,
        )

    def test_guessed_numeric_ticket_pk_does_not_resolve(self):
        self.assertEqual(self.client.get(f"/tickets/{self.db['ticket'].pk}/").status_code, 404)

    def test_foreign_policy_and_member_denied(self):
        self.assertEqual(
            self.client.get(
                reverse("desk:policy-detail", args=[self.db["policy"].uuid])
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                reverse("desk:member-edit", args=[self.da["policy"].uuid, self.db["member"].uuid])
            ).status_code,
            404,
        )

    def test_global_search_same_keyword_returns_current_company_only(self):
        response = self.client.get("/search/?q=Card")
        urls = [row["url"] for row in response.context["results"]]
        self.assertIn(reverse("desk:ticket-detail", args=[self.da["ticket"].uuid]), urls)
        self.assertNotIn(reverse("desk:ticket-detail", args=[self.db["ticket"].uuid]), urls)

    def test_htmx_search_fragment_is_scoped(self):
        response = self.client.get(
            "/tickets/?q=Card", HTTP_HX_REQUEST="true", HTTP_X_COMPANY_CONTEXT=str(self.a.uuid)
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, str(self.db["ticket"].uuid))
        self.assertEqual(response["X-Company-Context"], str(self.a.uuid))

    def test_foreign_attachment_and_document_downloads_denied(self):
        for kind in ["attachment", "document"]:
            self.assertEqual(
                self.client.get(
                    reverse("desk:download", args=[kind, self.db[kind].uuid])
                ).status_code,
                404,
            )

    def test_authorized_download_serves_file_and_is_not_public(self):
        response = self.client.get(
            reverse("desk:download", args=["attachment", self.da["attachment"].uuid])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), self.a.code.encode())
        self.assertEqual(
            self.client.get("/private-files/" + self.da["attachment"].file.name).status_code, 404
        )

    def test_storage_paths_include_company_uuid_despite_equal_filename(self):
        a, b = self.da["attachment"], self.db["attachment"]
        self.assertIn(str(self.a.uuid), a.file.name)
        self.assertIn(str(self.b.uuid), b.file.name)
        self.assertNotEqual(a.file.name, b.file.name)

    def test_cross_company_fk_rejected_by_model(self):
        with self.scope(), self.assertRaises(ValidationError):
            m.Policy.objects.create(
                policy_number="FORGED",
                organization=self.org_b,
                product=self.da["policy"].product,
                start_date=timezone.localdate(),
                end_date=timezone.localdate(),
            )

    def test_cross_company_user_assignment_rejected(self):
        with self.scope():
            ticket = m.Ticket.objects.get(pk=self.da["ticket"].pk)
            ticket.assigned_to = self.bob
            with self.assertRaises(ValidationError):
                ticket.save()

    def test_cross_company_group_membership_rejected(self):
        with self.scope(), self.assertRaises(ValidationError):
            m.SupportGroupMember.objects.create(
                group=self.da["category"].support_group, user=self.bob
            )

    def test_cross_company_bulk_update_rejected(self):
        with self.scope(), self.assertRaises(ValidationError):
            m.Ticket.objects.filter(pk=self.da["ticket"].pk).update(category=self.db["category"])

    def test_cross_company_bulk_create_rejected(self):
        with self.scope(), self.assertRaises(ValidationError):
            m.Policy.objects.bulk_create(
                [
                    m.Policy(
                        tenant=self.a,
                        policy_number="FORGED",
                        organization=self.org_b,
                        product=self.da["policy"].product,
                        start_date=timezone.localdate(),
                        end_date=timezone.localdate(),
                    )
                ]
            )

    def test_cross_company_fk_rejected_by_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE desk_policy SET organization_id = %s WHERE id = %s",
                    [self.org_b.pk, self.da["policy"].pk],
                )
                if connection.vendor == "postgresql":
                    cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

    def test_owner_cannot_be_reassigned_in_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE desk_policy SET tenant_id = %s WHERE id = %s",
                    [self.b.pk, self.da["policy"].pk],
                )

    def test_owner_cannot_be_reassigned_in_model(self):
        with self.scope():
            policy = m.Policy.objects.get(pk=self.da["policy"].pk)
            policy.tenant = self.b
            with self.assertRaises(PermissionDenied):
                policy.save()

    def test_raw_query_forbidden_on_scoped_manager(self):
        with self.scope(), self.assertRaises(PermissionDenied):
            m.Ticket.objects.raw("SELECT * FROM desk_ticket")

    def test_policy_form_choices_do_not_include_foreign_organizations(self):
        with self.scope():
            form = PolicyForm(tenant=self.a)
            self.assertNotIn(
                self.org_b.pk, form.fields["organization"].queryset.values_list("pk", flat=True)
            )

    def test_forged_ticket_form_category_rejected(self):
        response = self.post(
            "/tickets/new/",
            {
                "title": "Forged",
                "description": "Forged",
                "project": self.db["category"].product.project_id,
                "category": self.db["category"].pk,
                "request_type": "TICKET",
                "priority": "NORMAL",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assertFalse(m.Ticket.all_objects.filter(title="Forged").exists())

    def test_browser_tenant_field_cannot_override_ownership(self):
        response = self.post(
            "/tickets/new/",
            {
                "tenant": self.b.pk,
                "title": "Valid request",
                "description": "Created by the server",
                "project": self.da["category"].product.project_id,
                "category": self.da["category"].pk,
                "request_type": "TICKET",
                "priority": "NORMAL",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(m.Ticket.all_objects.get(title="Valid request").tenant_id, self.a.pk)

    def test_staff_alone_cannot_enter_a_company(self):
        self.client.force_login(self.staff)
        self.set_company(self.a)
        self.assertEqual(self.client.get("/tickets/").status_code, 302)
        self.assertEqual(self.client.get("/api/tickets/").status_code, 403)

    def test_session_tenant_forgery_denied(self):
        self.set_company(self.b)
        self.assertEqual(self.client.get("/api/tickets/").status_code, 403)

    def test_arbitrary_query_tenant_id_does_not_change_company(self):
        response = self.client.get(f"/api/tickets/?tenant_id={self.b.pk}")
        self.assertEqual(response.json()["company_uuid"], str(self.a.uuid))

    def test_unauthorized_switch_returns_404(self):
        self.assertEqual(
            self.post("/companies/switch/", {"company": str(self.b.uuid)}).status_code, 404
        )

    def test_invalid_company_uuid_fails_safely(self):
        self.assertEqual(
            self.post("/companies/switch/", {"company": "not-a-uuid"}).status_code, 404
        )

    def test_company_switch_discards_wizard_and_changes_role(self):
        self.client.force_login(self.dual)
        self.set_company(self.a)
        session = self.client.session
        session["ticket_wizard"] = {"private": "A"}
        session["filters"] = {"policy": self.da["policy"].pk}
        session.save()
        response = self.post("/companies/switch/", {"company": str(self.b.uuid)})
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("ticket_wizard", self.client.session)
        self.assertNotIn("filters", self.client.session)
        self.assertEqual(self.client.session["active_tenant_id"], self.b.pk)
        self.assertEqual(self.client.get("/tickets/new/").status_code, 403)
        response = self.client.get("/tickets/")
        self.assertEqual([row.pk for row in response.context["page"]], [self.db["ticket"].pk])

    def test_old_wizard_and_htmx_context_rejected_after_switch(self):
        self.client.force_login(self.dual)
        self.set_company(self.b)
        self.assertEqual(self.post("/tickets/new/", {"title": "Old wizard"}).status_code, 403)
        self.assertEqual(
            self.client.get(
                "/tickets/", HTTP_X_COMPANY_CONTEXT=str(self.a.uuid), HTTP_HX_REQUEST="true"
            ).status_code,
            403,
        )

    def test_unsafe_portal_form_requires_company_fingerprint(self):
        self.assertEqual(self.client.post("/tickets/new/", {}).status_code, 403)

    def test_cache_keys_include_company_user_and_permission_scope(self):
        with self.scope():
            a_key = tenant_cache_key("dashboard")
        with self.scope(self.b, self.bob):
            b_key = tenant_cache_key("dashboard")
        self.assertNotEqual(a_key, b_key)
        self.assertIn(str(self.a.uuid), a_key)

    def test_dashboard_cache_cannot_leak_other_company(self):
        with self.scope(self.a, self.alice):
            key = tenant_cache_key("dashboard")
            cache.set(key, {"open": 777, "completed": 0, "overdue": 0, "active_members": 0})
        self.client.force_login(self.bob)
        self.set_company(self.b)
        self.assertNotEqual(self.client.get("/").context["stats"]["open"], 777)

    def test_notifications_scoped_to_user_and_company(self):
        response = self.client.get("/notifications/")
        self.assertNotContains(response, "COMPANY_B PRIVATE")
        self.assertEqual(
            [row.pk for row in response.context["notifications"]], [self.da["notification"].pk]
        )

    def test_audit_list_contains_only_current_company(self):
        response = self.client.get("/audit/")
        self.assertNotIn(
            self.db["audit"].pk, [row["object"].pk for row in response.context["rows"]]
        )

    def test_audit_is_append_only_in_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE desk_auditevent SET action = %s WHERE id = %s",
                    ["forged", self.da["audit"].pk],
                )

    def test_rag_secret_does_not_cross_company(self):
        results = retrieve_knowledge(tenant=self.a, actor=self.alice, query="SECRET-COMPANY_B-789")
        self.assertFalse(any("SECRET-COMPANY_B-789" in row["content"] for row in results))

    def test_vector_ranking_cannot_include_foreign_candidates(self):
        results = retrieve_knowledge(tenant=self.a, actor=self.alice, query="any", embedding=[1, 0])
        self.assertTrue(results)
        self.assertTrue(all(row["tenant_id"] == self.a.pk for row in results))

    def test_ai_training_prompts_are_company_specific(self):
        with self.scope():
            prompt = resolve_prompt()
        self.assertIn("TRAINING-COMPANY_A-PRIVATE", prompt)
        self.assertNotIn("TRAINING-COMPANY_B-PRIVATE", prompt)

    def test_analytics_sql_only_sees_authorized_snapshot(self):
        result = execute_analytics(
            tenant=self.a, actor=self.alice, sql="SELECT COUNT(*) FROM policies"
        )
        self.assertEqual(result["rows"], [(1,)])

    def test_analytics_cannot_query_shared_tables_or_schema(self):
        for sql in [
            "SELECT * FROM desk_ticket",
            "SELECT * FROM sqlite_master",
            "SELECT load_extension('bad')",
            "SELECT * FROM tickets; DELETE FROM tickets",
        ]:
            with self.subTest(sql=sql), self.assertRaises(ValidationError):
                execute_analytics(tenant=self.a, actor=self.alice, sql=sql)

    def test_export_excludes_foreign_data(self):
        response = self.client.get("/reports/tickets.csv")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode("utf-8-sig").count("Card issue"), 1)

    def test_mailbox_configuration_and_email_admin_scoped(self):
        response = self.client.get("/settings/mailboxes/")
        self.assertNotContains(response, self.db["mailbox"].email_address)
        self.assertEqual(list(response.context["objects"])[0].pk, self.da["mailbox"].pk)
        self.assertNotIn(
            self.db["email"].pk, [row.pk for row in response.context["inbound_emails"]]
        )

    def test_email_authority_cannot_be_reused_across_companies(self):
        with self.scope(), self.assertRaises(PermissionDenied):
            resolve_email_authority(
                tenant=self.a,
                sender="authorized@example.com",
                policy=self.db["policy"],
                transaction_type="MEMBER_ADD",
            )
        with tenant_context(self.b, system=True):
            self.db["authority"].active = False
            self.db["authority"].save()
            self.assertIsNone(
                resolve_email_authority(
                    tenant=self.b,
                    sender="authorized@example.com",
                    policy=self.db["policy"],
                    transaction_type="MEMBER_ADD",
                )
            )

    def test_email_policy_lookup_uses_mailbox_company(self):
        with tenant_context(self.a, system=True):
            email = process_inbound(email=self.da["email"])
        self.assertEqual(email.policy_id, self.da["policy"].pk)
        self.assertEqual(email.state, "NEEDS_REVIEW")

    def test_worker_cannot_sync_foreign_mailbox(self):
        with tenant_context(self.a, system=True), self.assertRaises(PermissionDenied):
            sync_mailbox(mailbox=self.db["mailbox"])

    def test_secret_fields_reject_passwords(self):
        with self.scope():
            mailbox = self.da["mailbox"]
            mailbox.secret_env = "p@ssword-or-client-secret"
            with self.assertRaises(ValidationError):
                mailbox.save()

    def test_company_cannot_reference_another_company_secret(self):
        with self.scope():
            mailbox = self.da["mailbox"]
            mailbox.secret_env = self.db["mailbox"].secret_env
            with self.assertRaises(ValidationError):
                mailbox.save()

    def test_grant_is_limited_to_policy_and_resource(self):
        self.grant(["POLICY_VIEW"], {"policy": self.db["policy"].pk})
        self.assertIn(self.b.pk, allowed_tenants(self.alice).values_list("pk", flat=True))
        self.set_company(self.b)
        self.assertEqual(
            self.client.get(
                reverse("desk:policy-detail", args=[self.db["policy"].uuid])
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(reverse("desk:policy-edit", args=[self.db["policy"].uuid])).status_code,
            403,
        )
        self.assertEqual(self.client.get("/tickets/").status_code, 403)
        self.assertEqual(self.client.get("/settings/").status_code, 403)

    def test_grant_cannot_reference_resource_outside_target_company(self):
        with self.assertRaises(ValidationError):
            self.grant(["POLICY_VIEW"], {"policy": self.da["policy"].pk})

    def test_revoked_grant_stops_access_immediately(self):
        grant = self.grant(["POLICY_VIEW"])
        self.set_company(self.b)
        self.assertEqual(self.client.get("/api/policies/").status_code, 200)
        grant.active = False
        grant.save()
        self.assertEqual(self.client.get("/api/policies/").status_code, 403)

    def test_expired_grant_stops_access(self):
        self.grant(
            ["POLICY_VIEW"],
            valid_from=timezone.now() - timedelta(days=2),
            valid_until=timezone.now() - timedelta(days=1),
        )
        self.set_company(self.b)
        self.assertEqual(self.client.get("/api/policies/").status_code, 403)

    def test_group_grant_requires_membership_in_source_company(self):
        grant = TenantAccessGrant.objects.create(
            source_tenant=self.a,
            target_tenant=self.b,
            group=self.da["category"].support_group,
            scopes=["POLICY_VIEW"],
            reason="External group audit access",
            created_by=self.platform,
            approved_by=self.platform,
        )
        self.assertIn(self.b.pk, allowed_tenants(self.alice).values_list("pk", flat=True))
        with tenant_context(self.a, system=True):
            m.SupportGroupMember.objects.filter(group=grant.group, user=self.alice).update(
                active=False
            )
        self.assertNotIn(self.b.pk, allowed_tenants(self.alice).values_list("pk", flat=True))

    def test_platform_role_does_not_silently_enter_customer_company(self):
        self.client.force_login(self.platform)
        self.set_company(self.b)
        self.assertEqual(self.client.get("/api/policies/").status_code, 403)
        self.assertEqual(self.client.get("/admin/tenancy/tenant/").status_code, 200)

    def test_platform_support_requires_reason_and_is_audited(self):
        self.client.force_login(self.platform)
        self.set_company(self.b)
        response = self.client.post(
            "/companies/support/start/",
            {"company": str(self.b.uuid), "reason": "Investigate customer support request"},
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.get("/tickets/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Platform support access")
        self.assertTrue(
            PlatformAuditEvent.objects.filter(
                action="support.started", tenant=self.b, actor=self.platform
            ).exists()
        )
        self.assertLessEqual(
            SupportSession.objects.get(user=self.platform).expires_at - timezone.now(),
            timedelta(minutes=30),
        )

    def test_expired_platform_support_session_denied(self):
        self.client.force_login(self.platform)
        support = SupportSession.objects.create(
            user=self.platform,
            tenant=self.b,
            reason="Past support operation",
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        session = self.client.session
        session["active_tenant_id"], session["support_session_uuid"] = self.b.pk, str(support.uuid)
        session.save()
        self.assertEqual(self.client.get("/api/policies/").status_code, 403)

    def test_tenant_staff_admin_cannot_see_foreign_admin_objects(self):
        self.alice.is_staff = True
        self.alice.save()
        response = self.client.get("/admin/desk/policy/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row.pk for row in response.context["cl"].queryset], [self.da["policy"].pk]
        )
        self.assertEqual(
            self.client.get(f"/admin/desk/policy/{self.db['policy'].pk}/change/").status_code, 302
        )
        self.assertEqual(self.client.get("/admin/identity/user/").status_code, 403)

    def test_service_token_is_company_bound(self):
        token, digest = m.ServiceAccount.new_token()
        with self.scope():
            m.ServiceAccount.objects.create(
                name="Integration", token_hash=digest, created_by=self.alice, scopes=["ticket.view"]
            )
        client = Client()
        response = client.get("/api/tickets/", HTTP_AUTHORIZATION="Bearer " + token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["results"]), 1)
        response = client.get(
            "/api/tickets/", HTTP_AUTHORIZATION="Bearer " + token, HTTP_X_TENANT_ID=str(self.b.uuid)
        )
        self.assertEqual(response.status_code, 403)

    def test_service_token_cannot_exceed_its_scopes(self):
        token, digest = m.ServiceAccount.new_token()
        with self.scope():
            m.ServiceAccount.objects.create(
                name="Tickets only",
                token_hash=digest,
                created_by=self.alice,
                scopes=["ticket.view"],
            )
        self.assertEqual(
            Client().get("/api/policies/", HTTP_AUTHORIZATION="Bearer " + token).status_code, 403
        )

    def test_api_cookie_write_cannot_bypass_csrf(self):
        response = self.client.post(
            "/api/analytics/",
            json.dumps({"sql": "SELECT * FROM tickets"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_deactivated_company_denies_portal_token_and_jobs(self):
        token, digest = m.ServiceAccount.new_token()
        with self.scope():
            m.ServiceAccount.objects.create(
                name="Integration", token_hash=digest, created_by=self.alice, scopes=["ticket.view"]
            )
            job = queue_job(job_type="REPORT_EXPORT", actor=self.alice)
        self.a.active = False
        self.a.save()
        self.assertEqual(self.client.get("/api/tickets/").status_code, 403)
        self.assertEqual(
            Client().get("/api/tickets/", HTTP_AUTHORIZATION="Bearer " + token).status_code, 401
        )
        self.assertFalse(run_job(job_id=job.pk, tenant=self.a))

    def test_feature_flag_enforced_at_route_and_query(self):
        self.a.feature_flags = {"policies": False}
        self.a.save()
        self.assertEqual(self.client.get("/policies/").status_code, 404)
        self.assertEqual(self.client.get("/api/policies/").status_code, 404)
        with self.scope():
            self.assertFalse(m.Policy.objects.exists())

    def test_export_job_revalidates_revoked_membership(self):
        with self.scope():
            job = queue_job(job_type="REPORT_EXPORT", actor=self.alice)
        membership = TenantMembership.objects.get(tenant=self.a, user=self.alice)
        membership.active = False
        membership.save()
        self.assertFalse(run_job(job_id=job.pk, tenant=self.a))
        job.refresh_from_db()
        self.assertEqual(job.state, "FAILED")
        self.assertFalse(job.result_file)

    def test_job_payload_company_mismatch_rejected(self):
        with tenant_context(self.a, system=True), self.assertRaises(ValidationError):
            m.Job.objects.create(
                job_type="REPORT_EXPORT", actor=self.alice, payload={"tenant_id": self.b.pk}
            )

    def test_job_cannot_be_run_under_another_company(self):
        with self.scope():
            job = queue_job(job_type="REPORT_EXPORT", actor=self.alice)
        self.assertFalse(run_job(job_id=job.pk, tenant=self.b))

    def test_notification_email_rechecks_membership(self):
        with tenant_context(self.a, system=True):
            m.EmailOutbox.objects.create(
                recipient=self.alice,
                ticket=self.da["ticket"],
                subject="Private update",
                body="Private body",
            )
        member = TenantMembership.objects.get(tenant=self.a, user=self.alice)
        member.active = False
        member.save()
        with tenant_context(self.a, system=True), patch("apps.desk.jobs.send_mail") as send:
            self.assertEqual(send_outbox(tenant=self.a), 0)
            send.assert_not_called()

    def test_invitation_reuses_global_identity_and_preserves_other_role(self):
        with self.scope(self.b, self.bob):
            invitation, token = create_invitation(
                email=self.alice.email, role="AUDITOR", actor=self.bob
            )
        membership = accept_invitation(token=token, user=self.alice)
        self.assertEqual(membership.user_id, self.alice.pk)
        self.assertEqual(membership.role, "AUDITOR")
        self.assertEqual(TenantMembership.objects.get(tenant=self.a, user=self.alice).role, "ADMIN")
        self.assertEqual(get_user_model().objects.filter(email__iexact=self.alice.email).count(), 1)

    def test_invitation_cannot_be_accepted_by_another_identity(self):
        with self.scope():
            invitation, token = create_invitation(
                email=self.bob.email, role="AGENT", actor=self.alice
            )
        with self.assertRaises(PermissionDenied):
            accept_invitation(token=token, user=self.requester)

    def test_onboarding_idempotent(self):
        before = m.Category.all_objects.filter(tenant=self.a).count()
        onboard_company(code=self.a.code, name=self.a.name, admin_email=self.alice.email)
        self.assertEqual(m.Category.all_objects.filter(tenant=self.a).count(), before)
        self.assertEqual(TenantMembership.objects.filter(user=self.alice, tenant=self.a).count(), 1)

    def test_foreign_workflow_approval_denied(self):
        with tenant_context(self.b, system=True):
            approval = m.Approval.objects.create(ticket=self.db["ticket"], assigned_to=self.bob)
        self.assertEqual(
            self.post(
                reverse("desk:approval-decide", args=[approval.uuid]), {"decision": "APPROVED"}
            ).status_code,
            404,
        )

    def test_approval_requires_assignee_and_valid_workflow_state(self):
        with tenant_context(self.a, system=True):
            approval = m.Approval.objects.create(ticket=self.da["ticket"], assigned_to=self.alice)
        with self.assertRaises(ValidationError):
            decide_approval(
                tenant=self.a, actor=self.alice, approval_uuid=approval.uuid, decision="APPROVED"
            )

    def test_requester_cannot_advance_workflow(self):
        ticket = create_ticket(
            tenant=self.a,
            actor=self.requester,
            data={
                "title": "Own ticket",
                "description": "Test",
                "project": self.da["category"].product.project,
                "category": self.da["category"],
            },
        )
        with self.assertRaises(PermissionDenied):
            advance_ticket(tenant=self.a, actor=self.requester, ticket_uuid=ticket.uuid)

    def test_requester_only_sees_own_tickets(self):
        self.client.force_login(self.requester)
        self.set_company(self.a)
        self.assertEqual(
            self.client.get(
                reverse("desk:ticket-detail", args=[self.da["ticket"].uuid])
            ).status_code,
            404,
        )

    def test_append_only_audit_records_include_service_actor_and_company(self):
        ticket = create_ticket(
            tenant=self.a,
            actor=self.alice,
            data={
                "title": "Audit test",
                "description": "Test",
                "project": self.da["category"].product.project,
                "category": self.da["category"],
            },
        )
        audit = m.AuditEvent.all_objects.get(
            action="ticket.created", resource_uuid=str(ticket.uuid)
        )
        self.assertEqual(audit.tenant_id, self.a.pk)
        self.assertEqual(audit.actor_id, self.alice.pk)
        event = m.DomainEvent.all_objects.get(
            event_type="TicketCreated", payload__resource_uuid=str(ticket.uuid)
        )
        self.assertEqual(event.payload["tenant_id"], self.a.pk)

    def test_company_uuid_is_immutable_in_orm_and_database(self):
        previous = self.a.uuid
        self.a.uuid = uuid.uuid4()
        with self.assertRaises(ValidationError):
            self.a.save()
        self.a.uuid = previous
        with self.assertRaises(ValidationError):
            Tenant.objects.filter(pk=self.a.pk).update(uuid=uuid.uuid4())
        with self.assertRaises(IntegrityError), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE tenancy_tenant SET uuid = %s WHERE id = %s",
                    [uuid.uuid4().hex, self.a.pk],
                )
        self.assertEqual(Tenant.objects.get(pk=self.a.pk).uuid, previous)

    def test_platform_audit_database_blocks_update_and_delete(self):
        event = PlatformAuditEvent.objects.create(
            tenant=self.a, actor=self.platform, action="support.started"
        )
        for sql in [
            "UPDATE tenancy_platformauditevent SET action = 'changed' WHERE id = %s",
            "DELETE FROM tenancy_platformauditevent WHERE id = %s",
        ]:
            with self.subTest(sql=sql), self.assertRaises(IntegrityError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(sql, [event.pk])
        self.assertEqual(PlatformAuditEvent.objects.get(pk=event.pk).action, "support.started")

    def test_committed_foreign_file_reference_rejected(self):
        with self.scope(), self.assertRaises(ValidationError):
            self.da["attachment"].file = self.db["attachment"].file.name
            self.da["attachment"].save()

    def test_company_branding_cannot_reference_foreign_files(self):
        self.a.logo = self.db["attachment"].file.name
        with self.assertRaises(ValidationError):
            self.a.save()
        # Also refuse a malformed legacy row inserted outside the validated model path.
        Tenant.objects.filter(pk=self.a.pk).update(logo=self.db["attachment"].file.name)
        self.assertEqual(
            self.client.get(reverse("tenancy:branding", args=["logo"])).status_code, 404
        )

    def test_dashboard_configuration_range_and_cache_remain_scoped(self):
        self.a.configuration = {
            "dashboard": {"kpis": ["open"], "chart_type": "table", "default_range_days": 30}
        }
        self.a.save()
        with tenant_context(self.a, system=True):
            m.Ticket.objects.filter(pk=self.da["ticket"].pk).update(
                created_at=timezone.now() - timedelta(days=60)
            )
        recent = create_ticket(
            tenant=self.a,
            actor=self.alice,
            data={
                "title": "Recent own request",
                "description": "Private",
                "project": self.da["category"].product.project,
                "category": self.da["category"],
            },
        )
        response = self.client.get("/")
        self.assertEqual(response.context["stats"]["open"], 1)
        self.assertEqual([card["key"] for card in response.context["kpi_cards"]], ["open"])
        self.assertEqual([ticket.pk for ticket in response.context["recent_tickets"]], [recent.pk])
        self.assertContains(response, 'data-status-chart="table"')
        self.assertNotContains(response, str(self.db["ticket"].uuid))
        all_time = self.client.get("/?range=0")
        self.assertEqual(all_time.context["stats"]["open"], 2)
        self.assertEqual(self.client.get("/?range=invalid").status_code, 404)
        self.assertEqual(self.client.get("/?range=3651").status_code, 404)

    def test_dashboard_configuration_cannot_enable_denied_kpis(self):
        self.a.configuration = {"dashboard": {"kpis": ["active_members", "open"]}}
        self.a.save()
        self.client.force_login(self.requester)
        self.set_company(self.a)
        response = self.client.get("/")
        self.assertEqual([card["key"] for card in response.context["kpi_cards"]], ["open"])
        self.assertEqual(response.context["stats"]["active_members"], 0)
        self.assertNotContains(response, "Active members")

    def test_dashboard_settings_validate_supported_values(self):
        invalid = [
            {"kpis": ["all_companies"]},
            {"kpis": ["open", "open"]},
            {"kpis": [{}]},
            {"chart_type": "sql"},
            {"chart_type": {}},
            {"default_range_days": -1},
            {"default_range_days": True},
            {"tenant_id": self.b.pk},
        ]
        for configuration in invalid:
            with self.subTest(configuration=configuration), self.assertRaises(ValidationError):
                self.a.configuration = {"dashboard": configuration}
                self.a.full_clean()

    def test_offboarding_export_includes_only_own_records_and_files(self):
        token, digest = m.ServiceAccount.new_token()
        with self.scope():
            account = m.ServiceAccount.objects.create(
                name="Export test", token_hash=digest, created_by=self.alice, scopes=["ticket.view"]
            )
        self.a.configuration = {"dashboard": {"kpis": ["open"]}}
        self.a.status = "ARCHIVED"
        self.a.save()
        with tempfile.TemporaryDirectory() as directory:
            target = directory + "/company.zip"
            call_command(
                "export_company",
                company=self.a.code,
                output=target,
                include_files=True,
                stdout=io.StringIO(),
            )
            with zipfile.ZipFile(target) as archive:
                raw = archive.read("company.json").decode()
                payload = json.loads(raw)
                self.assertEqual(payload["company"]["uuid"], str(self.a.uuid))
                self.assertEqual(payload["company"]["configuration"], self.a.configuration)
                self.assertNotIn(self.bob.email, raw)
                self.assertNotIn("COMPANY_B", raw)
                self.assertNotIn("password", payload["users"][0])
                self.assertNotIn("token_hash", raw)
                self.assertNotIn(token, raw)
                self.assertNotIn(account.token_hash, raw)
                for name in archive.namelist():
                    if name != "company.json":
                        self.assertTrue(name.startswith(f"files/tenants/{self.a.uuid}/"))
                self.assertEqual(
                    archive.read("files/" + self.da["attachment"].file.name), self.a.code.encode()
                )
            with self.assertRaises(CommandError):
                call_command(
                    "export_company", company=self.a.code, output=target, stdout=io.StringIO()
                )
        self.assertTrue(
            PlatformAuditEvent.objects.filter(tenant=self.a, action="company.exported").exists()
        )

    def test_offboarding_missing_file_leaves_no_partial_export(self):
        with tempfile.TemporaryDirectory() as directory:
            from pathlib import Path

            target = Path(directory) / "company.zip"
            with (
                patch(
                    "django.core.files.storage.FileSystemStorage.open",
                    side_effect=FileNotFoundError,
                ),
                self.assertRaises(CommandError),
            ):
                call_command(
                    "export_company",
                    company=self.a.code,
                    output=str(target),
                    include_files=True,
                    stdout=io.StringIO(),
                )
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_all_portal_pages_render(self):
        paths = [
            "/",
            "/tickets/",
            "/tickets/new/",
            "/policies/",
            "/organizations/",
            "/tasks/",
            "/transactions/",
            "/documents/",
            "/knowledge/",
            "/audit/",
            "/settings/",
            "/reports/",
            "/ai/search/",
            "/companies/",
            "/notifications/",
            "/search/",
        ]
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn("no-store", response.get("Cache-Control", ""))
