# Operations

## Onboard a company

Use **Companies → Create company** as a platform operator, or `onboard_company` in the trusted deployment environment. The operation creates/reuses the company, one administrator identity/membership, eight organization types, the root organization, support group, standard SLA, workflows, GLIS/POL/ADD/DEL/CLM projects, products, categories and a notification template. Repeating it does not duplicate configuration or silently change an existing role.

New administrator identities have an unusable password until it is set with `manage.py changepassword`. Company administrators can invite other people from **Company settings**. Invitation links expire in seven days and are shown for deliberate sharing; the application does not send invitation messages automatically. New invitees create their identity during acceptance. Existing invitees sign in with the same global identity.

For multi-company access, create a separate membership and role. A user marked default in one membership enters that company first. Limited temporary exceptions are configured through platform **Tenant access grants**, with source/target company, principal, scopes, optional numeric resource restrictions and platform approval.

## Configure company workflows

Company settings contain projects, products, categories, SLA and workflows. Categories link their workflow, SLA and support group. Supported workflow stages are INTAKE, VALIDATION, APPROVAL, TPA and COMPLETE; stages are unique and must end in COMPLETE. General tickets use INTAKE → VALIDATION → COMPLETE. Medical workflows use the five stages. Rule JSON accepts only supported named rules; it never executes database-stored Python.

Before leaving APPROVAL, assign approvers and record approved decisions. Agents can decide only approvals assigned to them; managers/administrators have an explicit override. Before leaving TPA, linked transactions must be complete. Completing the last linked TPA transaction closes its ticket and updates a linked member for supported addition/deletion/termination operations.

`card_required`, `attachments_required` and `backdating_days` are enforced in relevant operations. `auto_approval_amount` and `tpa_required` are stored structured configuration for future automatic intake/routing; automatic premium approval is not enabled. Configure workflows without TPA where appropriate instead of assuming an arbitrary rule toggles workflow stages.

## Configure the dashboard

In Company settings, the Configuration JSON supports:

```json
{"dashboard": {"kpis": ["open", "completed", "overdue", "active_members"], "chart_type": "bar", "default_range_days": 30}}
```

Choose and order supported KPI cards, use `bar` or `table` for status distribution, and set a default range from 0 (all time) to 3650 days. The activity selector changes request counts, status distribution and recent requests; active members and pending approvals represent the current state. Module visibility uses feature flags. A configured KPI is hidden when the current role lacks its resource permission; configuration never widens access.

## Export one company

Run from the trusted deployment environment:

```bash
python manage.py export_company --company TAKAFUL_OMAN --output /secure/offboarding/takaful.zip --include-files
```

The export includes only that company's records, configuration, memberships, safe identity fields and optional files. Passwords and service-token hashes are excluded. Suspended or archived companies can be exported for offboarding. The archive contains sensitive customer data and needs the same access controls as private storage; it is not a portable automatic restore package.

## Microsoft Graph mailboxes

Create a mailbox under the company and configure Microsoft Graph application credentials with consent and mailbox-scoped access in Microsoft 365. The app uses application authentication and the inbox delta endpoint. Set Graph directory and client IDs, then store only the tenant-prefixed secret variable name in **Secret env**:

```text
TENANT_<COMPANY-UUID-HEX-IN-UPPERCASE>_GRAPH_SECRET
```

Set its value in `.env` or the worker's secret environment, then restart workers. The exact prefix is displayed beside secret fields. A company cannot select a different company's secret reference.

The mailbox determines ownership before policy lookup. Inbound emails are retained as NEEDS_REVIEW or UNAUTHORIZED. Same policy numbers in different companies resolve only within the mailbox company. Email authority requires company, email, policy/organization, transaction type and valid dates. Authority matching is not a substitute for mailbox-side anti-spoofing/authentication policies.

Poll once:

```bash
python manage.py sync_mailboxes --company TAKAFUL_OMAN
```

Schedule this command every minute using Task Scheduler, cron or your platform scheduler. Omitting `--company` deliberately dispatches each active company separately. A portal **Sync mailbox** action queues a one-time job instead. Deactivated companies and disabled mailboxes stop polling.

AI extraction reads subject/body and requires strict JSON with policy_number, transaction_type and a members array. A provider without permission to process sensitive company data is refused. Without an AI provider, deterministic policy/type extraction retains the email for review. Attachment OCR, automatic policy creation and zero-touch transaction execution are separate adapters; this release does not mark incomplete extraction as completed business processing.

## AI providers and knowledge

Configure a company-owned Ollama, OpenAI-compatible or Hugging Face chat-completion endpoint. Ollama expects its base URL; compatible providers expect the API prefix before `/chat/completions` (for example `https://router.huggingface.co/v1`). API keys use the tenant-prefixed token environment reference. Sensitive-data permission must be deliberately enabled for extraction. Vision support is recorded; no vision/OCR transport is implemented here.

Published company knowledge articles automatically create/update retrieval chunks with a company namespace. The built-in assistant performs scoped keyword retrieval; the retrieval service also accepts caller-supplied numeric vectors for scoped cosine ranking. It does not automatically call an embedding vendor or generate an ungrounded chat answer.

Reports expose the authorized snapshot SQL boundary. Raw application database credentials must never be handed to Vanna or another SQL-generating model. Connect generated SELECT statements to the snapshot service instead. There are no raw shared SQL tables available inside that boundary.

## Queued work and notifications

```bash
python manage.py run_tenant_jobs --watch
```

Workers poll queued jobs every 15 seconds and pass the company explicitly. Supported jobs are REPORT_EXPORT, MAILBOX_SYNC and EMAIL_SEND. Job payloads require the owning tenant_id. Queued exports revalidate the requester's role/grants at execution; downloads reject access changes after generation. Jobs for inactive companies remain retained without processing.

Category email notifications are off by default. When enabled, ticket updates create an outbox entry only for active members who opted into email. Browser notifications honor membership preference and show only the active company's notifications for the signed-in user. Internal note text is excluded from requester email history.

Configure SMTP and queue EMAIL_SEND under a company context (or call the reviewed outbox service from your scheduler). Recipient membership, ticket access and category notification setting are rechecked before sending. Development uses the console email backend. No external message is sent merely by seeding or testing the application.

## Suspend, archive and retain

Platform administration can set active=false or status=SUSPENDED/ARCHIVED. Portal/API access and worker entry stop, while records/files/audit remain. Reactivation restores valid memberships/grants. There is no generic tenant deletion action or automatic data destruction.

Back up PostgreSQL and private storage together. Platform backups contain all companies and need corresponding access controls. Optional offboarding exports can be produced with the trusted `export_company` command. Tenant-level restore is a separate reviewed process; ordinary ORM restore cannot change immutable ownership and must preserve relationships within the target company.

## Deploy behind HTTPS

Use PostgreSQL for production, an explicit long random secret, exact hosts/CSRF origins, secure cookies and a TLS reverse proxy. The Compose web port binds only to localhost. Gunicorn trusts forwarded HTTPS indicators only from its configured proxy peers; if your proxy uses another trusted internal address, configure Gunicorn's forwarded-allow-ips accordingly rather than exposing it to arbitrary clients.

Private storage must remain inaccessible to direct web-server URLs. WhiteNoise serves collected static assets only. `.env`, development database, uploads and demo passwords are excluded from version control and the Docker build.

Run migrations and collectstatic before starting the web/worker services. Keep a separate migration/operator identity where practical. Do not use the optional test database setting `POSTGRES_TEST_DB` against a live database.
