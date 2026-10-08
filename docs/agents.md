# Company AI agents

Each company has independent Claims, Policy, Finance and Ticketing configurations. An agent is a model binding plus its prompt, knowledge categories and registered tools. The model can request tools; Django decides whether each call is permitted for the current user and company.

## Configure providers and agents

Apply migrations before starting web and worker services. Migration `0004_company_ai_agents` seeds four agents for existing companies without selecting a provider. Company onboarding also creates these defaults. Repeating onboarding preserves custom settings.

As a company administrator:

1. Open **Company settings → AI providers**. Add an active provider with the model identifier that your server supports. Ollama uses its base URL, such as `http://localhost:11434`; OpenAI-compatible and Hugging Face endpoints use the API prefix before `/chat/completions`. In containers, use a hostname reachable from the worker instead of the web browser's localhost.
2. If authentication is required, set **Token env** to the company-prefixed environment variable name shown beside the field. Set its value in the worker environment and restart the worker. The database stores the reference, not the API key.
3. Enable **Allow sensitive data** only for a provider approved to process this company's records. Unapproved providers cannot run agents.
4. Open **Company settings → AI agents** and bind each agent to a provider. One provider/model can serve all four agents, or each agent can have a different provider/model. Select supported tools, role restrictions and comma-separated knowledge categories. Publish articles in those categories under **Knowledge**.
5. Run the worker:

```bash
python manage.py run_tenant_jobs --watch
# Optional: process just one company.
python manage.py run_tenant_jobs --watch --company TAKAFUL_OMAN
```

The existing Compose worker runs this command. Add the model credentials to that service's environment. The web request queues work; it does not make the browser wait for the model.

| Agent | Available registered tools | Default additional role restriction |
| --- | --- | --- |
| Claims | Search/read claim tickets, read a policy, search knowledge, propose a ticket comment | Existing resource permissions |
| Policy | Search/read policies, search members, search knowledge | Existing resource permissions |
| Finance | Summarize endorsement premium impacts, add decimal amounts, search knowledge | Administrator, Manager, Auditor |
| Ticketing | Search/read tickets, search knowledge, propose a ticket comment | Existing resource permissions |

Role restrictions only narrow existing access. A Claims user still needs policy access to read a policy. A Requester sees only their authorized tickets and external comments. Finance also needs report and transaction permissions for premium summaries. Empty knowledge categories disable knowledge retrieval; empty role restrictions use existing resource permissions. Tools unsupported by the selected domain are rejected when saving.

Set **Default** on one configuration per domain for automatic routing. Additional configurations may use the same domain with different prompts or models and be selected explicitly. Adding a new domain or tool requires a reviewed code change to the registry and its permission checks.

## Ask and review

Open **AI agents** in the portal. Cards show each available agent's model or **Setup needed**. Choose a specialist for a focused request, or choose **Automatic**.

Automatic routing matches business-area terms deterministically. For example, “Check claim CLM-2026-000001 and its policy” queues Claims and Policy. “Show recorded premium impacts” selects Finance. A question without a recognized area goes to Ticketing. If a requested area is unavailable, choose an agent you can use or ask an administrator to configure it.

Each selected agent receives an independent request and returns a separate answer. Agents do not share retrieved records or conversation memory. There is no model supervisor synthesizing their answers. Follow-up questions start new requests, so include the relevant reference again.

Requests progress through Queued, Running, Complete or Failed. The page refreshes results while work is pending. Open a completed request to inspect the tool trace and any draft comments. **Post comment** creates the reviewed external ticket comment through the normal permission-checked service. **Dismiss draft** discards the proposed action. Repeated submissions cannot post the same draft twice. No comment, policy change, claim approval or payment is automatically executed by a model.

Finance reports the stored `Transaction.premium_impact` totals in the company currency, optionally for one authorized policy. These are endorsement premium impacts, not collected premiums, paid claims, refunds issued or settlement balances. A payment ledger integration is required for those questions.

## Execution and access

The worker passes the company explicitly, restores the requesting user's current access, and rechecks membership, grants, feature flags, optional support-session expiry and agent/provider configuration around model and tool calls. A model cannot supply a company selector, credentials, raw SQL or executable code. Knowledge is filtered by company, publication, role and configured category before ranking.

Request history belongs to the requesting user and company. Access changes hide previous answers whose role/grant signature or referenced record access no longer matches. Company administrators can inspect their company's run/action records in read-only Django administration when staff access is enabled. Audit events record queuing, completion and draft review; traces contain tool names and outcomes, not raw provider errors or secrets.

The portable model protocol is a strict JSON envelope over the existing Ollama or compatible chat transport:

```json
{"answer":"","tool_calls":[{"name":"search_claims","arguments":{"query":"CLM-2026-000001"}}]}
```

The worker appends the scoped result as data and calls the same agent again. A final response uses `{"answer":"...","tool_calls":[]}`. Models must support this JSON format. Malformed output, unsupported tools and execution-budget exhaustion fail safely. Defaults allow four model steps, at most four tool calls per response, and a 120-second total model budget; administrators can set 1–8 steps and 10–180 seconds. Provider HTTP timeouts are capped by the remaining budget.

Failed requests show a safe error. Check the provider URL, model name, worker environment and JSON support. Requests left Running after a worker crash are marked Failed after five minutes when a dispatcher next polls; submit them again. This avoids automatically retrying partially completed work. Multiple workers can claim different requests on PostgreSQL; a request is claimed only once.

Email extraction uses the default agent's provider for the classified business area and layers the company's generic and transaction-specific extraction prompts. If classification changes the area, extraction runs once more with the matching provider/prompt. A single unbound legacy provider remains supported; multiple providers require an explicit agent binding.

## Verification

Run `python manage.py test apps.desk.tests.test_agents` for model binding, routing, tenant boundaries, object access, revocation, support expiry, scoped tools, reviewed drafts, interrupted workers and email prompt selection. The full suite also checks the existing workflows. Automated model responses are test fixtures; real model quality and credentials must be verified with your chosen providers.
