# Supplied requirement coverage

The source specification contains 80 sections. The application implements the isolation architecture and its operational entry points in this new repository. Optional infrastructure integrations are distinguished below rather than implied to exist.

| Requirement sections | Implementation |
|---|---|
| 1–4, 17–19, 48 | Separate Tenant, global User, independent membership roles/profiles, organization tree, default membership |
| 5, 12, 21–22, 36, 54, 60 | Verified session resolution, full company switching, server-owned tenant fields, form/HTMX fingerprints, scoped UUID lookups |
| 6, 51, 72, 74 | Platform/tenant administration separation, scoped Django admin, audited 30-minute support, suspension/archive instead of deletion |
| 7–11, 20, 26, 50, 61, 79–80 | Explicit ownership, default-deny managers, per-role/object authorization, tenant-aware services and shared schema |
| 8–9, 55 | Relationship validation, database composite keys/triggers, company-scoped uniqueness and equal-value regression fixtures |
| 13–16, 49, 53 | Approved user/group/service-account access grants; supported scopes, conjunctive resource restrictions, expiry/revocation; separate tenant support groups |
| 23–25, 58, 68–70 | Company search, scoped retrieval namespace, tenant prompt examples, isolated SQL snapshot, private knowledge/documents and explicit platform articles |
| 28–30, 65–67 | Company branding/preferences, configuration, notification footer, supported workflow/rule configuration; idempotent defaults copied into each company |
| 31–35, 37–43, 56, 59, 62 | Company-owned mailbox/authority/provider, credential namespace, private files, cache keys, explicit job/event company, scoped notifications/audit/reports/API |
| 44, 46 | Module flags in navigation/routes/querysets, active-company checks in requests, tokens and worker entry |
| 45, 47, 52 | Platform onboarding, accepted invitations with one global identity, mandatory cross-company security suite |
| 63–64 | Company/prefix/year counters, company-scoped references, opaque internal UUIDs |
| 75–76 | Trusted single-company offboarding ZIP command and documented database/storage backup boundary; restore remains a dedicated operations process |
| 77–78 | Concrete domain hierarchy and demo with Administrator in one company and Auditor in another |
| 27 | Optional database RLS is not enabled. Application isolation, composite FKs/triggers and snapshot SQL are implemented |
| 33 | Company-owned providers can point to the same infrastructure while retaining private prompts/context. A separate platform-provider catalog is optional and not enabled |
| 71 | Dashboard data is scoped and modules can be disabled. Custom per-company KPI layouts/date-range/chart-type configuration is implemented through validated dashboard configuration |
| 73 | Optional impersonation is not implemented. Support retains and displays the actual operator identity |

## Boundaries and extension points

The application is not a copy of GLIS's existing OCR/business automation engine. Mailbox polling, sender authority, safe JSON extraction, scoped prompts and service boundaries are implemented; attachment OCR, external embedding ingestion, automatic premium approval and zero-touch endorsement execution require separate adapters.

Current-company role checks are centralized in `apps/tenancy/access.py`. New capabilities/models must declare their resource and grant paths, inherit explicit ownership, use scoped managers, validate relationships, include tenant IDs in jobs/events and add cross-company tests. When adding/remaking business tables, preserve/reinstall the corresponding database guards in the migration.

No schema/database-per-company routing, infrastructure RLS, automatic external invitations, support impersonation or generic customer data destruction is enabled. These are optional deployment/lifecycle features from the supplied specification, not hidden authorization bypasses.
