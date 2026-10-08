import io
import json
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.desk import models as m
from apps.desk.agents import (
    execute_tool,
    expire_agent_runs,
    queue_agent_runs,
    review_agent_action,
    run_agent,
    seed_company_agents,
)
from apps.desk.ai import extract_email
from apps.tenancy.context import tenant_context
from apps.tenancy.models import SupportSession, TenantMembership

from . import test_tenancy as fixtures


def answer(text="Completed using authorized context."):
    return json.dumps({"answer": text, "tool_calls": []})


def tool(name, **arguments):
    return json.dumps({"answer": "", "tool_calls": [{"name": name, "arguments": arguments}]})


class AgentTests(TestCase):
    # Reuse the established two-company fixture, without inheriting/repeating its 87 tests.
    setUpTestData = classmethod(fixtures.TenantIsolationTests.setUpTestData.__func__)
    scope = fixtures.TenantIsolationTests.scope
    set_company = fixtures.TenantIsolationTests.set_company
    post = fixtures.TenantIsolationTests.post
    grant = fixtures.TenantIsolationTests.grant

    @classmethod
    def setUpClass(cls):
        cls.media = tempfile.mkdtemp(prefix="helpdesk-agent-tests-")
        cls.media_settings = override_settings(MEDIA_ROOT=cls.media)
        cls.media_settings.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.media_settings.disable()
        shutil.rmtree(cls.media, ignore_errors=True)

    def setUp(self):
        fixtures.TenantIsolationTests.setUp(self)
        self.configs = {}
        for tenant, actor in [(self.a, self.alice), (self.b, self.bob)]:
            with tenant_context(tenant, actor=actor, system=True):
                for config in m.AIAgentConfig.objects.all():
                    config.provider = m.AIProviderConfig.objects.create(
                        name=f"{tenant.code}-{config.code}",
                        model=f"model-{tenant.code}-{config.code}",
                        allow_sensitive_data=True,
                    )
                    config.save()
                    self.configs[tenant.code, config.code] = config

    def queue(self, code="TICKETING", tenant=None, actor=None, query="Find Card issue"):
        tenant, actor = tenant or self.a, actor or self.alice
        with self.scope(tenant, actor):
            return queue_agent_runs(tenant=tenant, actor=actor, selection=code, query=query)[0]

    def execute(self, responses, code="TICKETING", tenant=None, actor=None):
        run = self.queue(code, tenant, actor)
        with patch("apps.desk.agents.provider_completion", side_effect=responses) as provider:
            completed = run_agent(tenant=tenant or self.a, run_uuid=run.uuid)
        run.refresh_from_db()
        return run, completed, provider

    def draft(self, body="A reviewed draft."):
        run, completed, _ = self.execute(
            [
                tool("propose_ticket_comment", ticket_uuid=str(self.da["ticket"].uuid), body=body),
                answer("Draft ready for review."),
            ]
        )
        self.assertTrue(completed, run.error)
        return run, m.AIAgentAction.all_objects.get(run=run)

    def test_default_agents_are_company_owned_and_setup_is_idempotent(self):
        with tenant_context(self.a, actor=self.alice, system=True):
            seed_company_agents(tenant=self.a)
            self.assertEqual(
                set(m.AIAgentConfig.objects.values_list("code", flat=True)),
                {"CLAIMS", "POLICY", "FINANCE", "TICKETING"},
            )
            self.assertEqual(m.AIAgentConfig.objects.count(), 4)
        self.assertEqual(m.AIAgentConfig.all_objects.filter(tenant=self.b).count(), 4)

    def test_foreign_provider_rejected_by_model_and_database(self):
        config = self.configs[self.a.code, "CLAIMS"]
        foreign = self.configs[self.b.code, "CLAIMS"].provider
        with self.scope(), self.assertRaises(ValidationError):
            config.provider = foreign
            config.save()
        with self.assertRaises(IntegrityError), transaction.atomic():
            m.AIAgentConfig.all_objects.filter(pk=config.pk).update(provider_id=foreign.pk)
            connection.check_constraints()

    def test_run_agent_fk_is_company_consistent_in_database(self):
        run = self.queue()
        with self.assertRaises(IntegrityError), transaction.atomic():
            m.AIAgentRun.all_objects.filter(pk=run.pk).update(
                agent_id=self.configs[self.b.code, "TICKETING"].pk
            )
            connection.check_constraints()

    def test_draft_ticket_fk_is_company_consistent_in_database(self):
        _, draft = self.draft()
        with self.assertRaises(IntegrityError), transaction.atomic():
            m.AIAgentAction.all_objects.filter(pk=draft.pk).update(ticket_id=self.db["ticket"].pk)
            connection.check_constraints()

    def test_configuration_cannot_add_executable_or_other_domain_tools(self):
        config = self.configs[self.a.code, "CLAIMS"]
        for tools in [["execute_python"], ["premium_summary"]]:
            with self.scope(), self.assertRaises(ValidationError):
                config.allowed_tools = tools
                config.save()

    def test_domain_routing_uses_each_selected_model(self):
        with self.scope():
            runs = queue_agent_runs(
                tenant=self.a,
                actor=self.alice,
                selection="AUTO",
                query="Check claim policy premium and ticket",
            )
        self.assertEqual(
            {r.agent.domain for r in runs}, {"CLAIMS", "POLICY", "FINANCE", "TICKETING"}
        )
        used = []

        def complete(**kwargs):
            used.append(kwargs["provider"].model)
            return answer()

        with patch("apps.desk.agents.provider_completion", side_effect=complete):
            for run in runs:
                self.assertTrue(run_agent(tenant=self.a, run_uuid=run.uuid))
        self.assertEqual(
            set(used),
            {f"model-COMPANY_A-{code}" for code in ["CLAIMS", "POLICY", "FINANCE", "TICKETING"]},
        )

    def test_model_binding_is_selected_from_current_company(self):
        run, completed, provider = self.execute([answer()], "POLICY", self.b, self.bob)
        self.assertTrue(completed, run.error)
        self.assertEqual(provider.call_args.kwargs["provider"].tenant_id, self.b.pk)
        self.assertEqual(run.model_name, "model-COMPANY_B-POLICY")
        self.assertEqual(len(run.prompt_hash), 64)

    def test_agents_may_share_one_model_with_separate_prompts(self):
        with self.scope():
            claims = self.configs[self.a.code, "CLAIMS"]
            claims.provider = self.configs[self.a.code, "POLICY"].provider
            claims.save()
        run, completed, provider = self.execute([answer()], "CLAIMS")
        self.assertTrue(completed, run.error)
        self.assertEqual(run.model_name, "model-COMPANY_A-POLICY")
        self.assertIn("authorized claim", provider.call_args.kwargs["messages"][0]["content"])

    def test_missing_provider_is_actionable_and_creates_no_run(self):
        with self.scope():
            config = self.configs[self.a.code, "CLAIMS"]
            config.provider = None
            config.save()
            with self.assertRaisesMessage(ValidationError, "Configure an active provider"):
                queue_agent_runs(
                    tenant=self.a, actor=self.alice, selection="CLAIMS", query="Summarize claims"
                )
        self.assertFalse(m.AIAgentRun.all_objects.exists())

    def test_provider_sensitive_data_opt_in_required(self):
        with self.scope():
            provider = self.configs[self.a.code, "CLAIMS"].provider
            provider.allow_sensitive_data = False
            provider.save()
        with self.assertRaises(ValidationError):
            self.queue("CLAIMS")

    def test_user_cannot_route_to_finance_or_widen_role(self):
        with self.assertRaises(PermissionDenied):
            self.queue("FINANCE", actor=self.requester)
        with self.scope(actor=self.requester), self.assertRaises(ValidationError):
            queue_agent_runs(
                tenant=self.a, actor=self.requester, selection="AUTO", query="Show finance payments"
            )

    def test_module_flags_disable_agent_queueing(self):
        self.a.feature_flags = {"claims": False}
        self.a.save()
        with self.assertRaises(PermissionDenied):
            self.queue("CLAIMS")
        response = self.client.get(reverse("desk:agent-assistant"))
        self.assertNotContains(response, "Claims agent")

    def test_claim_tool_cannot_return_other_company_or_normal_tickets(self):
        run, completed, provider = self.execute(
            [tool("get_claim", ticket_uuid=str(self.db["ticket"].uuid)), answer("Not available.")],
            "CLAIMS",
        )
        self.assertTrue(completed, run.error)
        self.assertEqual(run.trace[0]["status"], "DENIED")
        self.assertNotIn(
            self.db["ticket"].description,
            json.dumps(provider.call_args_list[-1].kwargs["messages"]),
        )
        run, completed, _ = self.execute(
            [tool("get_claim", ticket_uuid=str(self.da["ticket"].uuid)), answer("Not a claim.")],
            "CLAIMS",
        )
        self.assertTrue(completed)
        self.assertEqual(run.trace[0]["status"], "DENIED")

    def test_current_company_lookup_and_requester_object_scope(self):
        with self.scope():
            self.da["ticket"].assigned_to = self.requester
            self.da["ticket"].save()
        run, completed, provider = self.execute(
            [
                tool("get_ticket", ticket_uuid=str(self.da["ticket"].uuid)),
                answer("Authorized ticket."),
            ],
            actor=self.requester,
        )
        self.assertTrue(completed, run.error)
        self.assertIn(
            self.da["ticket"].description,
            json.dumps(provider.call_args_list[-1].kwargs["messages"]),
        )
        with self.scope():
            self.da["ticket"].assigned_to = None
            self.da["ticket"].save()
        self.client.force_login(self.requester)
        self.set_company(self.a)
        self.assertEqual(
            self.client.get(reverse("desk:agent-run", args=[run.uuid])).status_code, 404
        )

    def test_disabled_claims_never_reach_ticketing_tools(self):
        with self.scope():
            self.da["ticket"].request_type = "CLAIM"
            self.da["ticket"].save()
        self.a.feature_flags = {"claims": False}
        self.a.save()
        run, completed, provider = self.execute(
            [tool("search_tickets", query="Card issue"), answer("No records.")]
        )
        self.assertTrue(completed, run.error)
        self.assertNotIn(
            self.da["ticket"].description,
            json.dumps(provider.call_args_list[-1].kwargs["messages"]),
        )
        self.assertIn(
            '"matching_count": 0', provider.call_args_list[-1].kwargs["messages"][-1]["content"]
        )

    def test_knowledge_filtered_by_company_domain_and_publication(self):
        for tenant, actor, category, body, published in [
            (self.a, self.alice, "Claims", "Claim ALLOWED-A", True),
            (self.a, self.alice, "Finance", "Claim FINANCE-SECRET", True),
            (self.a, self.alice, "Claims", "Claim DRAFT-SECRET", False),
            (self.b, self.bob, "Claims", "Claim COMPANY-B-SECRET", True),
        ]:
            with tenant_context(tenant, actor=actor, system=True):
                m.KnowledgeArticle.objects.create(
                    title="Claim process", category=category, body=body, published=published
                )
        run, completed, provider = self.execute(
            [tool("search_knowledge", query="claim"), answer("Grounded answer.")], "CLAIMS"
        )
        self.assertTrue(completed, run.error)
        sent = json.dumps(provider.call_args_list[-1].kwargs["messages"])
        self.assertIn("ALLOWED-A", sent)
        for secret in ["FINANCE-SECRET", "DRAFT-SECRET", "COMPANY-B-SECRET"]:
            self.assertNotIn(secret, sent)

    def test_empty_knowledge_scope_does_not_mean_all_categories(self):
        with self.scope():
            agent = self.configs[self.a.code, "CLAIMS"]
            agent.knowledge_categories = []
            agent.save()
            data = execute_tool(
                agent=agent, name="search_knowledge", arguments={"query": "Card"}, proposals=[]
            )
        self.assertEqual(data["results"], [])

    def test_unknown_tool_and_forged_tenant_arguments_fail_closed(self):
        for response in [
            tool("execute_sql", sql="SELECT * FROM desk_ticket"),
            tool("search_tickets", query="", tenant_id=self.b.pk),
        ]:
            run, completed, _ = self.execute([response])
            self.assertFalse(completed)
            self.assertEqual(run.status, "FAILED")
            self.assertEqual(run.answer, "")

    def test_revoked_membership_prevents_worker_model_call(self):
        run = self.queue()
        TenantMembership.objects.filter(tenant=self.a, user=self.alice).update(active=False)
        with patch("apps.desk.agents.provider_completion") as provider:
            self.assertFalse(run_agent(tenant=self.a, run_uuid=run.uuid))
        provider.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, "FAILED")

    def test_access_change_during_provider_call_discards_answer(self):
        run = self.queue()

        def revoke(**kwargs):
            TenantMembership.objects.filter(tenant=self.a, user=self.alice).update(active=False)
            return answer("Private data that must not be returned.")

        with patch("apps.desk.agents.provider_completion", side_effect=revoke):
            self.assertFalse(run_agent(tenant=self.a, run_uuid=run.uuid))
        run.refresh_from_db()
        self.assertEqual(run.answer, "")
        self.assertNotIn("Private data", run.error)

    def test_config_revocation_during_provider_call_stops_tools(self):
        run = self.queue()

        def disable(**kwargs):
            m.AIAgentConfig.all_objects.filter(pk=run.agent_id).update(active=False)
            return tool(
                "propose_ticket_comment",
                ticket_uuid=str(self.da["ticket"].uuid),
                body="Do not create this draft",
            )

        with patch("apps.desk.agents.provider_completion", side_effect=disable):
            self.assertFalse(run_agent(tenant=self.a, run_uuid=run.uuid))
        self.assertFalse(m.AIAgentAction.all_objects.exists())

    def test_permission_changes_hide_old_run_history(self):
        run, completed, _ = self.execute([answer("Administrator context.")])
        self.assertTrue(completed)
        TenantMembership.objects.filter(tenant=self.a, user=self.alice).update(role="USER")
        response = self.client.get(reverse("desk:agent-run", args=[run.uuid]))
        self.assertEqual(response.status_code, 404)
        self.assertNotContains(
            self.client.get(reverse("desk:agent-history")), "Administrator context."
        )

    def test_other_actor_and_company_cannot_read_run_history(self):
        run, _, _ = self.execute([answer("Private run.")])
        self.client.force_login(self.dual)
        self.set_company(self.a)
        self.assertEqual(
            self.client.get(reverse("desk:agent-run", args=[run.uuid])).status_code, 404
        )
        self.set_company(self.b)
        self.assertEqual(
            self.client.get(reverse("desk:agent-run", args=[run.uuid])).status_code, 404
        )

    def test_grant_revocation_stops_queued_cross_company_agent(self):
        grant = self.grant(["TICKET_VIEW"], {"ticket": self.db["ticket"].pk})
        run = self.queue(tenant=self.b, actor=self.alice)
        grant.active = False
        grant.save()
        with patch("apps.desk.agents.provider_completion") as provider:
            self.assertFalse(run_agent(tenant=self.b, run_uuid=run.uuid))
        provider.assert_not_called()

    def test_wrong_company_worker_cannot_claim_run(self):
        run = self.queue()
        with patch("apps.desk.agents.provider_completion") as provider:
            self.assertFalse(run_agent(tenant=self.b, run_uuid=run.uuid))
        provider.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, "QUEUED")

    def test_deactivated_company_does_not_execute_run(self):
        run = self.queue()
        self.a.active = False
        self.a.save()
        with patch("apps.desk.agents.provider_completion") as provider:
            self.assertFalse(run_agent(tenant=self.a, run_uuid=run.uuid))
        provider.assert_not_called()

    def test_expired_support_cannot_execute_queued_agent(self):
        session = SupportSession.objects.create(
            tenant=self.a,
            user=self.platform,
            reason="Review company agent",
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        with tenant_context(self.a, actor=self.platform, role="ADMIN", support=True):
            run = queue_agent_runs(
                tenant=self.a, actor=self.platform, selection="TICKETING", query="Summarize tickets"
            )[0]
        SupportSession.objects.filter(pk=session.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        with patch("apps.desk.agents.provider_completion") as provider:
            self.assertFalse(run_agent(tenant=self.a, run_uuid=run.uuid))
        provider.assert_not_called()

    def test_premium_summary_and_decimal_math_are_scoped(self):
        for tenant, actor, data, amount in [
            (self.a, self.alice, self.da, "123.456"),
            (self.b, self.bob, self.db, "999.000"),
        ]:
            with tenant_context(tenant, actor=actor, system=True):
                m.Transaction.objects.create(
                    ticket=data["ticket"],
                    policy=data["policy"],
                    member=data["member"],
                    transaction_type="MEMBER_ADD",
                    effective_date=timezone.localdate(),
                    premium_impact=amount,
                )
        with self.scope():
            config = self.configs[self.a.code, "FINANCE"]
            summary = execute_tool(agent=config, name="premium_summary", arguments={}, proposals=[])
            total = execute_tool(
                agent=config,
                name="calculate_total",
                arguments={"amounts": ["0.1", "0.2", "-0.05"]},
                proposals=[],
            )
            with self.assertRaises(ValidationError):
                execute_tool(
                    agent=config,
                    name="calculate_total",
                    arguments={"amounts": ["__import__('os')"]},
                    proposals=[],
                )
        self.assertEqual(Decimal(summary["premium_impact_total"]), Decimal("123.456"))
        self.assertEqual(summary["record_count"], 1)
        self.assertIn("No payment ledger", summary["limitation"])
        self.assertEqual(total["total"], "0.25")

    def test_draft_requires_review_and_repeated_post_is_idempotent(self):
        before = m.Comment.all_objects.filter(ticket=self.da["ticket"]).count()
        run, draft = self.draft()
        self.assertEqual(m.Comment.all_objects.filter(ticket=self.da["ticket"]).count(), before)
        for _ in range(2):
            response = self.post(
                reverse("desk:agent-review", args=[draft.uuid]), {"decision": "post"}
            )
            self.assertEqual(response.status_code, 302)
        self.assertEqual(m.Comment.all_objects.filter(ticket=self.da["ticket"]).count(), before + 1)
        draft.refresh_from_db()
        self.assertIsNotNone(draft.applied_at)
        self.assertEqual(run.actor_id, self.alice.pk)

    def test_dismiss_draft_does_not_post_and_cannot_later_be_posted(self):
        _, draft = self.draft()
        with self.scope():
            review_agent_action(
                tenant=self.a, actor=self.alice, action_uuid=draft.uuid, decision="dismiss"
            )
            review_agent_action(
                tenant=self.a, actor=self.alice, action_uuid=draft.uuid, decision="post"
            )
        self.assertFalse(m.Comment.all_objects.filter(body=draft.body).exists())
        draft.refresh_from_db()
        self.assertIsNotNone(draft.dismissed_at)

    def test_draft_review_revalidates_role_and_actor(self):
        _, draft = self.draft()
        self.client.force_login(self.dual)
        self.set_company(self.a)
        self.assertEqual(
            self.post(
                reverse("desk:agent-review", args=[draft.uuid]), {"decision": "post"}
            ).status_code,
            404,
        )
        self.client.force_login(self.alice)
        TenantMembership.objects.filter(tenant=self.a, user=self.alice).update(role="AUDITOR")
        self.assertEqual(
            self.post(
                reverse("desk:agent-review", args=[draft.uuid]), {"decision": "post"}
            ).status_code,
            403,
        )
        self.assertFalse(m.Comment.all_objects.filter(body=draft.body).exists())

    def test_agent_unsafe_forms_require_company_context_and_csrf(self):
        self.assertEqual(
            self.client.post(
                reverse("desk:agent-assistant"), {"agent": "TICKETING", "query": "Help"}
            ).status_code,
            403,
        )
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.alice)
        self.set_company(self.a, client)
        self.assertEqual(
            client.post(
                reverse("desk:agent-assistant"),
                {"_company_context": str(self.a.uuid), "agent": "TICKETING", "query": "Help"},
            ).status_code,
            403,
        )

    def test_worker_processes_only_selected_company_and_does_not_repeat(self):
        a, b = self.queue(), self.queue(tenant=self.b, actor=self.bob)
        with patch("apps.desk.agents.provider_completion", return_value=answer()) as provider:
            call_command("run_tenant_jobs", company=self.a.code, stdout=io.StringIO())
            call_command("run_tenant_jobs", company=self.a.code, stdout=io.StringIO())
        self.assertEqual(provider.call_count, 1)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.status, "COMPLETE")
        self.assertEqual(b.status, "QUEUED")

    def test_invalid_output_step_budget_and_errors_are_safe(self):
        for responses in [
            ["not json"],
            [json.dumps({"answer": "Unexpected shape"})],
            [RuntimeError("SECRET_TOKEN private provider payload")],
        ]:
            run, completed, _ = self.execute(responses)
            self.assertFalse(completed)
            self.assertNotIn("SECRET_TOKEN", run.error)
        with self.scope():
            config = self.configs[self.a.code, "TICKETING"]
            config.max_steps = 1
            config.save()
        run, completed, _ = self.execute([tool("search_tickets", query="")])
        self.assertFalse(completed)
        self.assertEqual(run.status, "FAILED")

    def test_portal_agent_configuration_and_output_escaping(self):
        response = self.client.get(
            reverse(
                "desk:config-edit", args=["ai-agents", self.configs[self.a.code, "CLAIMS"].uuid]
            )
        )
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertNotIn(
            self.configs[self.b.code, "CLAIMS"].provider.pk,
            form.fields["provider"].queryset.values_list("pk", flat=True),
        )
        run, completed, _ = self.execute([answer("<script>alert('x')</script>")])
        self.assertTrue(completed)
        response = self.client.get(reverse("desk:agent-run", args=[run.uuid]))
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>alert")

    def test_stale_admin_form_cannot_write_into_switched_company(self):
        self.dual.is_staff = True
        self.dual.save()
        TenantMembership.objects.filter(tenant=self.b, user=self.dual).update(role="ADMIN")
        self.client.force_login(self.dual)
        self.set_company(self.a)
        response = self.client.get("/admin/desk/project/add/")
        self.assertContains(response, 'name="_company_context"')
        self.client.post(reverse("tenancy:switch"), {"company": str(self.b.uuid)})
        response = self.client.post(
            "/admin/desk/project/add/",
            {
                "code": "OLD_A",
                "name": "A confidential form",
                "active": "on",
                "_company_context": str(self.a.uuid),
                "_save": "Save",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(m.Project.all_objects.filter(code="OLD_A").exists())

    def test_company_admin_form_saves_with_valid_context(self):
        self.alice.is_staff = True
        self.alice.save()
        response = self.post(
            "/admin/desk/project/add/",
            {"code": "SAME_A", "name": "Valid company form", "active": "on", "_save": "Save"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(m.Project.all_objects.filter(code="SAME_A", tenant=self.a).exists())

    def test_email_selects_bound_policy_model_and_type_prompt(self):
        with tenant_context(self.a, actor=self.alice, system=True):
            m.AIPrompt.objects.create(
                purpose="EXTRACTION",
                transaction_type="MEMBER_ADD",
                prompt="TYPE_SPECIFIC_INSTRUCTIONS",
            )
            email = self.da["email"]
            email.subject = "Addition endorsement"
            payload = json.dumps(
                {
                    "policy_number": self.da["policy"].policy_number,
                    "transaction_type": "MEMBER_ADD",
                    "members": [],
                }
            )
            with patch("apps.desk.ai.provider_completion", return_value=payload) as provider:
                extract_email(email)
        self.assertEqual(provider.call_args.kwargs["provider"].model, "model-COMPANY_A-POLICY")
        self.assertIn(
            "TYPE_SPECIFIC_INSTRUCTIONS", provider.call_args.kwargs["messages"][0]["content"]
        )

    def test_requester_can_view_and_review_own_draft(self):
        with self.scope():
            self.da["ticket"].assigned_to = self.requester
            self.da["ticket"].save()
        run, completed, _ = self.execute(
            [
                tool(
                    "propose_ticket_comment",
                    ticket_uuid=str(self.da["ticket"].uuid),
                    body="Requester draft",
                ),
                answer("Draft ready"),
            ],
            actor=self.requester,
        )
        self.assertTrue(completed, run.error)
        draft = m.AIAgentAction.all_objects.get(run=run)
        self.client.force_login(self.requester)
        self.set_company(self.a)
        page = self.client.get(reverse("desk:agent-run", args=[run.uuid]))
        self.assertContains(page, "Requester draft")
        self.assertContains(page, "Post comment")
        self.assertEqual(
            self.post(
                reverse("desk:agent-review", args=[draft.uuid]), {"decision": "post"}
            ).status_code,
            302,
        )
        self.assertTrue(
            m.Comment.all_objects.filter(ticket=self.da["ticket"], body="Requester draft").exists()
        )

    def test_model_time_budget_is_enforced_after_call(self):
        clock = [0.0]
        run = self.queue()

        def late(**kwargs):
            clock[0] = 121.0
            return answer("Too late")

        with (
            patch("apps.desk.agents.time.monotonic", side_effect=lambda: clock[0]),
            patch("apps.desk.agents.provider_completion", side_effect=late),
        ):
            self.assertFalse(run_agent(tenant=self.a, run_uuid=run.uuid))
        run.refresh_from_db()
        self.assertEqual(run.answer, "")

    def test_interrupted_worker_recovery_respects_company_filter(self):
        a, b = self.queue(), self.queue(tenant=self.b, actor=self.bob)
        m.AIAgentRun.all_objects.filter(pk__in=[a.pk, b.pk]).update(
            status="RUNNING", started_at=timezone.now() - timedelta(minutes=6)
        )
        self.assertEqual(expire_agent_runs(company_code=self.a.code), 1)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.status, "FAILED")
        self.assertEqual(b.status, "RUNNING")
        self.assertIn("Submit it again", a.error)

    def test_expired_worker_cannot_persist_drafts_or_success(self):
        run = self.queue()

        def stopped(**kwargs):
            m.AIAgentRun.all_objects.filter(pk=run.pk).update(
                started_at=timezone.now() - timedelta(minutes=6)
            )
            expire_agent_runs(company_code=self.a.code)
            return answer("Draft ready")

        with patch("apps.desk.agents.provider_completion") as provider:
            provider.side_effect = lambda **kwargs: (
                tool(
                    "propose_ticket_comment",
                    ticket_uuid=str(self.da["ticket"].uuid),
                    body="Expired draft",
                )
                if provider.call_count == 1
                else stopped(**kwargs)
            )
            self.assertFalse(run_agent(tenant=self.a, run_uuid=run.uuid))
        run.refresh_from_db()
        self.assertEqual(run.status, "FAILED")
        self.assertEqual(run.answer, "")
        self.assertFalse(m.AIAgentAction.all_objects.filter(run=run).exists())

    def test_history_polls_only_while_requests_are_pending(self):
        response = self.client.get(reverse("desk:agent-assistant"))
        self.assertNotContains(response, 'hx-trigger="every 3s"')
        run = self.queue()
        response = self.client.get(reverse("desk:agent-history"))
        self.assertContains(response, 'hx-trigger="every 3s"')
        with patch("apps.desk.agents.provider_completion", return_value=answer()):
            self.assertTrue(run_agent(tenant=self.a, run_uuid=run.uuid))
        response = self.client.get(reverse("desk:agent-history"))
        self.assertNotContains(response, 'hx-trigger="every 3s"')

    def test_agent_history_admin_is_scoped_and_read_only(self):
        run, completed, _ = self.execute([answer()])
        self.assertTrue(completed)
        self.alice.is_staff = True
        self.alice.save()
        response = self.client.get("/admin/desk/aiagentrun/")
        self.assertEqual(response.status_code, 200)
        response = self.client.get(f"/admin/desk/aiagentrun/{run.pk}/change/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="_save"')
        foreign, _, _ = self.execute([answer()], tenant=self.b, actor=self.bob)
        self.assertEqual(
            self.client.get(f"/admin/desk/aiagentrun/{foreign.pk}/change/").status_code, 302
        )

    def test_unknown_email_reclassifies_with_type_specific_provider(self):
        with tenant_context(self.a, actor=self.alice, system=True):
            m.AIPrompt.objects.create(
                purpose="EXTRACTION",
                transaction_type="MEMBER_ADD",
                prompt="SECOND_STAGE_TYPE_PROMPT",
            )
            email = self.da["email"]
            email.subject, email.body = "Request", "Please review attached details."
            payload = json.dumps(
                {
                    "policy_number": self.da["policy"].policy_number,
                    "transaction_type": "MEMBER_ADD",
                    "members": [],
                }
            )
            with patch("apps.desk.ai.provider_completion", return_value=payload) as provider:
                extract_email(email)
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(
            provider.call_args_list[0].kwargs["provider"].model, "model-COMPANY_A-TICKETING"
        )
        self.assertEqual(
            provider.call_args_list[1].kwargs["provider"].model, "model-COMPANY_A-POLICY"
        )
        self.assertIn(
            "SECOND_STAGE_TYPE_PROMPT", provider.call_args_list[1].kwargs["messages"][0]["content"]
        )
