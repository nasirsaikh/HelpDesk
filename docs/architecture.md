# Architecture and authorization

`Tenant` is the company security boundary; `Organization` is a business entity inside it. Global `User` identities have independent `TenantMembership` records. A global staff or Django permission flag does not grant business data access.

## Ownership and database checks

Every business model inherits `TenantOwnedModel`, with a non-null, protected tenant foreign key, opaque UUID, tenant index and `(tenant,id)` unique key. Human references, policy numbers, organization/project codes, plans and counters have company-scoped uniqueness.

Model saves validate ownership, capabilities, all company-owned foreign keys and newly assigned user memberships. Scoped bulk creation, updates and deletion use the same validation. Historical actors remain retained after their membership ends. Company ownership cannot be reassigned.

SQLite triggers reject cross-company foreign keys and ownership changes. PostgreSQL uses composite foreign keys on `(tenant_id, related_id)` and immutable-ownership triggers. Business and platform audit histories are append-only. Tenant UUIDs are immutable in both application and database layers.

## Default-deny queries

The `objects` manager is always scoped to an authorized `ContextVar` company context. Without one, it returns an empty queryset and writes fail. Consequently, even `Ticket.objects.all()` is scoped; it is never a global business queryset.

```python
Ticket.objects.for_tenant(request.tenant)
```

`all_objects` is reserved for reviewed infrastructure: credential lookup, platform dispatch, onboarding, invitation-token lookup and authorized report snapshots. It must never be introduced into normal URL lookups or searches. Related-object base managers are global so Django can preserve historical relationships; same-company foreign keys are independently validated and constrained in the database.

`tenant_context(..., system=True)` is a trusted infrastructure boundary, not an authorization shortcut for application views. Normal business services accept `tenant` and `actor` explicitly and revalidate active membership or grants before entering their context.

## Request resolution

Middleware runs after authentication. It resolves a verified session selection, a valid default membership or the first allowed company. Invalid or revoked selections fail closed. Browser query parameters never select a tenant. Tenant-bound service credentials can use a tenant header only after an explicit target grant is validated.

Unsafe portal forms carry the current company UUID. HTMX requests also carry `X-Company-Context`. Switching regenerates the session key, removes transient/wizard/filter state and performs a full navigation. Old-company forms and fragments are rejected. Browser responses use `private, no-store`; browser back/forward cache restoration reloads the page.

Company timezone and language are applied during each request. UI labels currently use English, with RTL layout for Arabic settings; a complete Arabic translation catalog can be added without changing the authorization model.

## Role capabilities

| Role | Read scope | Mutation scope |
|---|---|---|
| Administrator | Current-company operational data and settings | Current-company administration and operations |
| Operations manager | Current-company operational data, reports and audit | Operational work and manager approval overrides |
| Support agent | Current-company operational work | Operational work; approvals require assignment |
| Requester | Own/assigned tickets, own notifications, published knowledge and company documents | Own ticket creation/comments/uploads; cannot advance workflows |
| Auditor | Current-company operational data, reports and audit | None |

These capabilities are combined with object rules. For example, a pending approval must be in the approval stage and assigned to its deciding agent; only a company manager/administrator may override that assignment. Global Django groups are not used as business support groups.

## Cross-company exceptions

`TenantAccessGrant` requires an active source identity, a target company, supported scopes, a reason and platform approval. The principal is exactly one user, source-company support group or service account. Optional restrictions are conjunctive: project, product, organization, policy and/or ticket. Unsupported resource paths deny access instead of silently widening it.

Membership in the source company, source group membership, activation and expiry are rechecked during request resolution. A POLICY_VIEW grant does not expose tickets, settings or editing routes. Regular ongoing work in another company should use a membership with its own role.

Platform support requires explicit action, a reason and a 30-minute session. Actual operator and target tenant are recorded in platform audit, and the portal shows a persistent support banner. There is no user impersonation.

## AI, files, caches and jobs

- Private files use `tenants/<immutable UUID>/...` paths and are served only through authorized object lookups. No public media route exists.
- Cache keys include company UUID, actor, role, service account and effective grant scope.
- Company secret references must begin `TENANT_<UUID-HEX-IN-UPPERCASE>_`. Administrators cannot select another company's process secret.
- Knowledge candidates are filtered by company and publication before lexical/cosine ranking. Every vector row carries a company namespace.
- Prompt resolution combines the safe platform prompt with current-company purpose/type overrides and training examples.
- Analytics SQL sees only allowlisted columns copied from authorized querysets into an isolated SQLite database. Raw tables, schema tables, extension loading, mutation and multi-statements are rejected; execution and results are bounded.
- Workers dispatch globally but enter one explicit company context per job. Exports revalidate the actor at execution and again at download; permission changes invalidate previously generated exports.
- Mailbox ownership determines inbound email ownership before policy lookup. Authority matching uses company, sender, policy/organization, transaction type and validity dates.
- Feature flags gate routes, navigation and ordinary scoped queries. Tenant deactivation blocks requests, tokens and worker entry.

RLS is optional defense in depth and is not enabled in this release. Do not connect an LLM or external analytics identity to raw shared tables. The snapshot service is the supported SQL boundary. PostgreSQL application credentials should have no schema-owner or bypass privileges beyond those required by the deployment role; use a separate migration role where operationally practical.
