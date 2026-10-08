# MULTI-COMPANY / MULTI-TENANT ARCHITECTURE — MANDATORY

The application must be designed as a **true multi-company SaaS / enterprise multi-tenant platform**.

This is a fundamental security and data-architecture requirement.

A company using the system must operate as though it has its own private installation of GLIS.

Example:

Tenant / Company A:
Takaful Oman

Tenant / Company B:
Insurance Company B

Tenant / Company C:
Insurance Company C

Takaful Oman users must not see Company B or Company C data unless an explicit cross-company access grant has been created.

This rule applies to:

- users
- groups
- organizations
- policies
- members
- tickets
- tasks
- claims
- endorsements
- transactions
- documents
- inbound emails
- AI configuration
- analytics
- dashboards
- notifications
- knowledge articles
- reports
- workflows
- SLA
- projects
- products
- categories
- audit records
- scheduled jobs
- API access
- exports
- attachments
- search
- background workers
- caches

The application must use **default-deny tenant isolation**.

---

# 1. TENANT IS NOT THE SAME AS ORGANIZATION

Create a dedicated top-level model:

Company

or preferably internally:

Tenant

The UI may call it:

Company

Do not use `Organization` itself as the tenant boundary.

The hierarchy is:

PLATFORM
→ COMPANY / TENANT
→ ORGANIZATIONS
→ USERS / GROUPS / POLICIES / REQUESTS / OPERATIONS

Example:

Platform
└── Takaful Oman
    ├── Takaful Oman Insurance Company
    ├── Muscat Branch
    ├── Salalah Branch
    ├── Corporate Client A
    ├── Corporate Client B
    ├── Broker A
    ├── TPA A
    └── Healthcare Providers

Another tenant may contain its completely separate organization tree.

This distinction is critical.

A Tenant is a **hard security boundary**.

An Organization is a **business entity within a tenant**.

---

# 2. COMPANY / TENANT MODEL

Create:

Tenant

Recommended fields:

- id
- uuid
- code
- name
- legal_name
- short_name
- slug
- active
- logo
- favicon
- primary_color
- secondary_color
- default_language
- default_timezone
- default_currency
- country
- contact_email
- contact_phone
- address
- registration_number
- tax_registration_number
- domain
- configuration JSON
- feature_flags JSON
- subscription/plan metadata if required
- created_at
- updated_at

Example:

code:
TAKAFUL_OMAN

name:
Takaful Oman Insurance SAOG

slug:
takaful-oman

Every tenant must have an immutable internal identifier.

Do not use company name as a security key.

---

# 3. TENANT MEMBERSHIP

Create:

TenantMembership

Fields:

- tenant
- user
- role
- active
- is_default
- joined_at
- valid_from
- valid_until
- created_by
- configuration

A user may belong to:

- one tenant
- multiple tenants
- no tenant, if platform administrator

Example:

User:
Nasir

Memberships:

Takaful Oman
    role = Administrator

Company B
    role = Auditor

This user can deliberately switch between those companies.

Membership must never automatically expose data belonging to another tenant.

---

# 4. DEFAULT COMPANY

A user may have:

default_tenant

When the user logs in:

1. if exactly one active tenant membership exists:
   enter that tenant automatically

2. if multiple memberships exist:
   enter default tenant

3. optionally allow tenant selection after login

The active tenant becomes part of the authenticated portal context.

Example:

request.tenant

All tenant-owned queries must derive their scope from:

request.tenant

Never from a tenant ID supplied blindly by the browser.

---

# 5. COMPANY SWITCHER

If a user belongs to multiple companies, show a Company Switcher in the portal header/profile menu.

Example:

Takaful Oman
Company B
Company C

Switching company changes the active tenant context.

The entire portal should then refresh into that tenant.

After switching:

- dashboard changes
- navigation changes
- tickets change
- policies change
- members change
- tasks change
- organizations change
- notifications change
- search changes
- AI config changes
- analytics change
- branding may change

The browser must never retain stale records from the previous tenant.

Clear tenant-sensitive HTMX fragments and client-side caches during switching.

---

# 6. PLATFORM ADMINISTRATION

Create two different administrative concepts.

## PLATFORM ADMIN

Can operate across tenants.

Examples:

- SaaS owner
- global system operator
- infrastructure administrator

Permissions may include:

- create tenant
- deactivate tenant
- access tenant configuration
- inspect platform health
- perform explicitly authorized support operations

Platform admin access must still be audited.

Do not make ordinary Django `is_staff` automatically equivalent to unrestricted tenant data access.

## TENANT ADMIN

Administrator of one company.

Can administer:

- users belonging to that tenant
- organizations
- policies
- projects
- products
- categories
- groups
- SLA
- workflows
- AI configuration
- mailbox configuration
- tenant branding

But cannot access other tenants.

---

# 7. TENANT OWNERSHIP ON DATA

Every tenant-owned business record must have an explicit tenant relationship.

Examples:

Organization
    tenant

Policy
    tenant

Member
    tenant

Ticket
    tenant

Task
    tenant

TPATransaction
    tenant

InboundEmail
    tenant

Document
    tenant

KnowledgeArticle
    tenant

Notification
    tenant

Project
    tenant

Product
    tenant

Category
    tenant

SupportGroup
    tenant

AIProviderConfig
    tenant

MailboxConfig
    tenant

SLA
    tenant

Workflow
    tenant

Job
    tenant

AuditEvent
    tenant

Do not attempt to infer tenant by traversing:

ticket.policy.organization.tenant

for normal authorization.

Store tenant explicitly on security-sensitive root entities.

This makes authorization easier to audit and dramatically reduces cross-tenant mistakes.

---

# 8. TENANT-SCOPED FOREIGN KEYS

All relationships must be validated to belong to the same tenant.

Example:

A Ticket belonging to Takaful Oman must not reference:

- Company B policy
- Company B organization
- Company B user without explicit allowed cross-company participation
- Company B category
- Company B project

Validation must occur server-side.

Example service validation:

ticket.tenant == policy.tenant

ticket.tenant == category.tenant

ticket.tenant == project.tenant

Do not rely on filtered HTML selects as the security check.

---

# 9. DATABASE CONSTRAINTS

Where possible enforce tenant consistency at database and application level.

Unique constraints should normally include tenant.

Wrong:

unique policy_number globally

Better:

UniqueConstraint(
    fields=["tenant", "policy_number"]
)

Wrong:

unique project code globally

Better:

tenant + project code

Examples:

tenant + policy_number
tenant + project_code
tenant + organization_code
tenant + category_code
tenant + benefit_plan_code within policy
tenant + ticket_reference where appropriate

Some platform identifiers may remain globally unique.

---

# 10. TENANT QUERYSETS

Create tenant-aware managers/querysets.

Example conceptual API:

Ticket.objects.for_tenant(tenant)

Policy.objects.for_tenant(tenant)

Organization.objects.for_tenant(tenant)

Do not repeatedly write ad-hoc:

.filter(tenant=request.tenant)

across hundreds of views.

Build reusable scoping infrastructure.

Possible structure:

services/tenancy/
    context.py
    middleware.py
    managers.py
    access.py
    guards.py

---

# 11. NEVER USE UNSCOPED QUERYSETS IN BUSINESS VIEWS

Portal code must avoid:

Ticket.objects.all()

Policy.objects.all()

Member.objects.all()

Organization.objects.all()

for tenant-facing operations.

Use:

Ticket.objects.for_tenant(request.tenant)

or:

TicketAccessPolicy.visible_queryset(
    user=request.user,
    tenant=request.tenant
)

A global unscoped queryset should only appear in explicitly reviewed platform-level administration code.

---

# 12. TENANT MIDDLEWARE

After authentication resolve:

request.tenant

from secure session/server-side membership state.

Do not trust:

?tenant_id=123

as sufficient proof.

The selected tenant must be verified against active TenantMembership or an explicit platform-level privilege.

Example sequence:

authenticate user
→ load allowed tenants
→ resolve selected tenant
→ validate membership
→ set request.tenant
→ continue request

If invalid:

403 or redirect to tenant selector.

---

# 13. CROSS-COMPANY ACCESS

Cross-company access must be possible, but only as an explicit exception.

Support grants to:

- individual user
- group
- service account
- integration identity

Create:

TenantAccessGrant

Possible fields:

- source/target tenant
- user nullable
- group nullable
- permission scope
- resource scope
- valid_from
- valid_until
- active
- created_by
- reason
- approved_by
- created_at

Example:

Nasir normally belongs to Takaful Oman.

Give Nasir access to Company B as:

VIEW_ONLY

or:

POLICY_AUDITOR

or:

SUPPORT_ADMIN

Do not simply set:

can_access_all_tenants = True

except for genuine platform operators.

---

# 14. PREFERRED CROSS-COMPANY MODEL

For regular users, prefer adding explicit TenantMembership when the user genuinely works in multiple companies.

Example:

Nasir:
- Takaful Oman Administrator
- Company B Auditor

Use TenantAccessGrant for narrower exceptional access such as:

- temporary audit
- support intervention
- specific project
- specific policy
- specific ticket
- particular support group

This creates clearer authorization semantics.

---

# 15. GRANULAR CROSS-COMPANY SCOPES

The access-grant mechanism should support scopes such as:

TENANT_VIEW
TENANT_ADMIN
TICKET_VIEW
TICKET_EDIT
POLICY_VIEW
POLICY_ADMIN
REPORT_VIEW
AUDIT_VIEW
SUPPORT
APPROVAL
TPA_PROCESSING

Optionally scope further by:

- project
- organization
- policy
- ticket
- product

Example:

User from Takaful Oman may have access to:

Company B
only
Project = CLAIMS
and
Permission = VIEW

They must not automatically see Company B's policies, payroll, TPA, or documents.

---

# 16. GROUP ACCESS ACROSS TENANTS

SupportGroup belongs to a tenant.

Example:

Takaful Oman
    Motor Support
    Medical Operations

Company B
    Medical Support

A group must never accidentally contain users from an unrelated tenant unless explicitly defined as a cross-tenant group.

If cross-tenant operational groups are needed, create a separate concept such as:

SharedSupportGroup

or grant external users access to the target tenant's support group through explicit membership.

Do not silently mix tenant-local group semantics.

---

# 17. ORGANIZATION MODEL WITHIN TENANT

Change Organization to:

Organization
- tenant
- organization_type
- parent
- code
- name
- ...

Organization unique constraints must be tenant scoped.

Organization relationships must remain inside tenant unless an explicitly supported inter-company relationship exists.

For example:

Takaful Oman Tenant
→ Takaful Oman Insurance Company
→ Branch
→ Corporate policyholder
→ Broker
→ TPA

Company B will have its own completely separate organizations.

---

# 18. USERS

A platform User is global identity.

A user's company-specific properties should not all be stored directly on User.

Instead use membership/profile models.

Example:

User
    email
    global authentication identity

TenantMembership
    tenant
    user
    role
    employee_number
    department
    job_title
    manager
    active
    language
    local settings

Why:

The same person could be:

Administrator in Takaful Oman

but:

Auditor in Company B.

Do not store one universal:

user.role

that applies to every company.

---

# 19. TENANT-SPECIFIC PROFILE

Where business attributes differ by tenant, use:

TenantUserProfile

or extend TenantMembership.

Fields may include:

- employee ID
- department
- designation
- manager
- tenant-specific role
- active
- browser notifications
- email notification preference
- delegated authority
- default support group

Global user preferences may remain on User/Profile.

---

# 20. AUTHORIZATION CONTRACT

Every protected operation evaluates at least:

USER
+
ACTIVE TENANT
+
TENANT MEMBERSHIP / ACCESS GRANT
+
RESOURCE TENANT
+
ROLE/PERMISSION
+
OBJECT-SPECIFIC ACCESS
+
BUSINESS RULE

Example:

Can Nasir approve Ticket X?

Check:

1. authenticated
2. current tenant valid
3. Ticket X belongs to active tenant
4. Nasir belongs to tenant or has explicit access grant
5. Nasir has approval permission in that tenant
6. Nasir is the assigned approver or authorized override
7. workflow currently allows approval
8. approval not already decided

Only then allow.

---

# 21. DEFAULT DENY

The core rule is:

NO TENANT MATCH
=
NO ACCESS

unless an explicit valid cross-company authorization exists.

Do not attempt to determine access later in the template.

Deny it before the object is returned from the database whenever possible.

---

# 22. TENANT SECURITY IN URL LOOKUPS

Unsafe:

Ticket.objects.get(pk=pk)

then later:

if ticket.tenant != request.tenant:
    ...

Preferred:

get_object_or_404(
    TicketAccessPolicy.visible_queryset(
        request.user,
        request.tenant
    ),
    pk=pk
)

Users should ideally receive a generic 404 for resources completely outside their accessible tenant boundary.

This reduces information disclosure.

---

# 23. SEARCH IS TENANT SCOPED

Global search must only search current tenant data.

When current tenant is Takaful Oman:

Search:

ABC

must NEVER return Company B records.

Search indices must include tenant identity.

If using Elasticsearch/OpenSearch/vector database:

every indexed record must carry:

tenant_id

Every query must enforce:

tenant_id = active tenant

before similarity/ranking.

---

# 24. VECTOR / AI TENANT ISOLATION

This is especially important.

Vector embeddings must not mix tenant context in retrieval.

Preferred options:

A.
separate vector collection/namespace per tenant

or

B.
mandatory tenant metadata filter on every retrieval

Example:

collection:
glis_takaful_oman

or metadata:

tenant_id = <uuid>

Never send Company B documents as context while answering a Takaful Oman user's question.

This applies to:

- Vanna
- RAG
- Knowledge search
- AI document search
- AI analytics context

---

# 25. VANNA / TEXT-TO-SQL TENANT SECURITY

A natural-language analytics query must never remove tenant constraints.

Preferred architecture:

Expose tenant-filtered database views.

For example:

analytics_tickets_current_tenant

or inject secure database-side tenant context.

At minimum, SQL validation must require tenant-safe sources.

Do not allow an LLM to generate:

SELECT * FROM tickets

against raw shared tables without tenant enforcement.

Prefer database views/security policies that make cross-company access impossible even if generated SQL is imperfect.

---

# 26. DATABASE TENANCY STRATEGY

Recommended initial architecture:

SHARED DATABASE
+
SHARED SCHEMA
+
MANDATORY tenant_id

This is suitable because:

- easier operations
- easier upgrades
- easier reporting
- easier explicit cross-company user access
- simpler shared platform code
- lower infrastructure cost

However design application services so the platform could later support:

- database-per-tenant
- schema-per-tenant

for customers requiring stronger infrastructure isolation.

Do not hard-code assumptions that make future isolation impossible.

---

# 27. OPTIONAL DATABASE-LEVEL ROW SECURITY

Where supported and operationally appropriate, add database-level row isolation in addition to Django authorization.

For PostgreSQL this may use RLS.

For SQL Server investigate Row-Level Security / security predicates.

Application-level tenant filtering remains mandatory.

Database-level security is defense in depth.

Do not rely solely on middleware.

---

# 28. TENANT BRANDING

Each company can configure:

- logo
- favicon
- company name
- short name
- portal title
- primary color
- secondary color
- login branding
- email branding
- report branding
- date/time preferences
- currency
- timezone
- default language

Example:

Takaful Oman users see:

Takaful Oman branding

Company B users see:

Company B branding

Do not require code changes for branding.

---

# 29. TENANT-SPECIFIC CONFIGURATION

The following must normally belong to Tenant:

- Project
- Product
- Category
- SLA
- Workflow
- SupportGroup
- TicketReferenceConfiguration
- OrganizationType override if supported
- AIProviderConfig
- AI prompts
- extraction profiles
- mailbox
- email authority
- notification templates
- ticket templates
- document configuration
- knowledge categories
- integrations
- feature flags

Do not accidentally share Takaful Oman operational configuration with another company.

---

# 30. OPTIONAL GLOBAL TEMPLATES

The platform may provide global defaults.

For example:

Global Project Template:
Medical Endorsement

A tenant can clone it.

Do not let tenants directly mutate global templates.

Architecture:

PlatformTemplate

→ clone

TenantConfiguration

This prevents one tenant's changes impacting others.

---

# 31. MAILBOX ISOLATION

MailboxConfig belongs to tenant.

Example:

Takaful Oman mailbox:

endorsement@takafuloman.om

Company B:

medical@companyb.com

Each mailbox worker operation must explicitly carry tenant.

Inbound email processing must create data only under its mailbox tenant.

Do not infer tenant only from extracted policy number.

Mailbox configuration is the primary tenant boundary.

---

# 32. EMAIL AUTHORITY IS TENANT SCOPED

TPAEmailAuthority must contain:

tenant

Authorization matching:

tenant
+
email
+
organization
+
policy
+
transaction type

A sender authorized for Takaful Oman must not automatically be authorized for Company B merely because the same email address exists.

---

# 33. AI PROVIDER ISOLATION

Allow two modes.

## Tenant AI Provider

Takaful Oman may configure its own:

- Ollama
- Hugging Face
- Azure/OpenAI
- internal endpoint

Company B can configure another provider.

## Platform AI Provider

Platform can optionally provide shared infrastructure.

Even when infrastructure is shared:

prompts
documents
retrieval
logs
training examples
usage

remain tenant-scoped.

Never mix prompt training examples between companies unless explicitly published as platform templates.

---

# 34. FILE STORAGE ISOLATION

Store files using tenant-aware paths.

Example:

tenants/
    <tenant_uuid>/
        tickets/
        policies/
        documents/
        emails/

Not:

media/tickets/123/file.pdf

Use unpredictable/internal paths as appropriate.

Before serving any file:

verify tenant and object permission.

Do not depend on obscurity of storage URL.

---

# 35. CACHE ISOLATION

Every tenant-sensitive cache key must include tenant ID.

Unsafe:

ticket-dashboard

Safe:

tenant:<uuid>:ticket-dashboard:<user-or-scope>

This applies to:

- dashboard
- permissions
- search
- configuration
- navigation
- analytics
- counts
- AI retrieval cache

Prevent cross-company cache poisoning/leakage.

---

# 36. SESSION ISOLATION

Session stores current tenant ID.

When switching:

validate membership again.

Clear:

- tenant-specific cached state
- wizard state
- stored filter state if unsafe
- pending HTMX context
- server-side transient states tied to previous tenant where applicable

Do not allow a wizard started under Company A to be submitted after switching to Company B.

Wizard/session keys must include tenant.

---

# 37. BACKGROUND JOB ISOLATION

Every background job must know:

tenant_id

Example job payload:

{
    "tenant_id": "...",
    "job_type": "MAILBOX_SYNC",
    ...
}

Worker must reload tenant and scope all queries.

Never execute:

InboundEmail.objects.filter(state="NEW")

globally unless the job is explicitly platform-level.

Instead process per tenant/mailbox.

---

# 38. NOTIFICATION ISOLATION

Notification belongs to tenant.

A user's notification dropdown shows only:

active tenant notifications

unless a separate platform notification area is deliberately provided.

Tenant switch refreshes unread count.

---

# 39. AUDIT ISOLATION

Every business audit event contains:

tenant_id

Platform audit can aggregate across tenants only for explicitly authorized platform operators.

Tenant admin can only see own tenant audit.

Cross-tenant support access must itself generate audit events.

Example:

"Platform support user accessed Takaful Oman tenant."

---

# 40. ANALYTICS ISOLATION

Dashboards run entirely in current tenant context.

Takaful Oman dashboards must contain only Takaful Oman data.

Company B dashboard must contain Company B data.

Platform administrators may have a separate:

Platform Analytics

surface.

Do NOT place platform-wide analytics in ordinary tenant dashboard routes.

---

# 41. REPORT EXPORTS

Export jobs must carry:

tenant
user
permissions
filters

Revalidate access when generating the export.

Do not assume permission remains valid because the export was initially requested.

Generated export storage is tenant-scoped.

---

# 42. API TENANCY

Every API request must resolve tenant securely.

Options:

- authenticated user's active tenant
- tenant-bound API key/service account
- explicitly validated tenant header for authorized multi-tenant integrations

Never accept arbitrary:

X-Tenant-ID

without verifying the caller has access.

API credentials should normally belong to a specific tenant.

---

# 43. TENANT SERVICE ACCOUNTS

Support:

ServiceAccount

Fields:

- tenant
- name
- active
- API credentials
- allowed scopes
- allowed IP/network optional
- expiry
- last_used
- created_by

A Takaful Oman API credential must not access Company B.

---

# 44. TENANT FEATURE FLAGS

Each tenant may enable/disable modules.

Example:

Takaful Oman:

Tickets = Yes
TPA = Yes
AI = Yes
Analytics = Yes
Claims = Yes

Company B:

Tickets = Yes
TPA = No
AI = No

Navigation and endpoints must both enforce this.

Hiding menu alone is not sufficient.

---

# 45. COMPANY ONBOARDING

Build platform-level tenant onboarding.

Flow:

1. Create Company
2. Configure branding
3. Configure timezone/currency/language
4. Create tenant administrator
5. Seed organization types
6. Create root organization
7. Create default support groups
8. Create initial projects/products/categories
9. Configure SLA
10. Configure mailbox
11. Configure AI provider
12. Configure integrations
13. Invite users
14. Activate tenant

Make onboarding repeatable and idempotent.

---

# 46. TENANT DEACTIVATION

When tenant is deactivated:

- users cannot enter tenant
- API credentials stop
- mailbox jobs stop
- scheduled tenant jobs stop
- normal background processing stops
- data remains retained
- audit remains accessible to authorized platform operator
- no destructive automatic deletion

Provide controlled reactivation.

---

# 47. USER INVITATION

Tenant administrator can invite a user.

If email already exists globally:

do not create duplicate identity.

Instead add TenantMembership after appropriate invitation acceptance.

Example:

nasir@example.com already belongs to Takaful Oman.

Company B invites same email.

After accepting:

same User identity
+
new Company B membership

Permissions remain separate.

---

# 48. COMPANY-SPECIFIC ROLES

Do not make role global.

Correct:

TenantMembership:
    role = ADMIN for Takaful Oman

TenantMembership:
    role = AUDITOR for Company B

Do not store:

User.role = ADMIN

and assume that across all tenants.

---

# 49. TENANT-SCOPED DJANGO GROUPS

Django's standard Group is global.

Do not rely directly on global Django Group names for business authorization such as:

"Medical Team"

because two tenants can have the same logical group.

Use a tenant-owned model:

SupportGroup

or:

TenantGroup

with optional mapping to Django permissions.

Example:

Takaful Oman / Medical Operations

Company B / Medical Operations

They are different records.

---

# 50. TENANT-SCOPED PERMISSIONS

Use Django permissions for capabilities:

tickets.change_ticket
tpa.approve_transaction

and tenant membership/access policy for scope.

Permission alone must never grant cross-tenant data access.

Conceptually:

Capability
×
Tenant Scope
×
Object Scope

determines authorization.

---

# 51. TENANT-AWARE ADMIN

Prefer a dedicated platform administration area for tenant management.

Django Admin used by Tenant Admin must also be scoped.

Do not allow a tenant staff user to use Django Admin and accidentally see all organizations/users/policies because the ModelAdmin queryset was not filtered.

Every tenant-owned ModelAdmin must implement tenant filtering or be restricted to platform admins.

Also filter foreign-key selectors.

---

# 52. SECURITY TESTS — MULTI-TENANCY

Create mandatory cross-tenant tests.

Create:

Tenant A
Tenant B

User A belongs only to A.

User B belongs only to B.

Test User A cannot:

- list Tenant B tickets
- open Tenant B ticket by UUID
- guess numeric PK
- search Tenant B
- download Tenant B attachment
- access Tenant B policy
- access Tenant B member
- access Tenant B transaction
- assign Tenant B user
- select Tenant B organization
- approve Tenant B workflow
- query Tenant B analytics
- retrieve Tenant B vector documents
- call Tenant B API
- receive Tenant B notifications
- export Tenant B reports
- see Tenant B audit
- inspect Tenant B mailbox
- access Tenant B admin objects

Every test must fail safely.

---

# 53. CROSS-TENANT GRANT TESTS

Also test intentional access.

Example:

User A belongs to Tenant A.

Grant:

Tenant B
POLICY_VIEW only

User A may:

view permitted Tenant B policy

but cannot:

edit policy
see unrelated tickets
see tenant settings
approve requests
download unrelated documents

Then expire/revoke grant.

Access must immediately stop.

---

# 54. TENANT SWITCH TEST

User belongs to A and B.

Test:

1. enter Tenant A
2. open A dashboard
3. search results are only A
4. begin create-ticket wizard
5. switch to B
6. old wizard cannot submit
7. dashboard now B
8. global search now B
9. notification count now B
10. direct cached A fragments are not visible
11. attempting A URL while active B is denied unless explicitly cross-authorized

---

# 55. DATA LEAKAGE TESTING

Create automated regression tests specifically designed to detect data leakage.

Include equal values across tenants.

Example:

Both tenants have:

policy number:
P/100/2026/001

organization name:
ABC LLC

member ID:
1001

This proves filtering is actually by tenant rather than accidental uniqueness.

---

# 56. STORAGE LEAKAGE TEST

Create same attachment filename in both tenants:

passport.pdf

Verify:

Tenant A URL/session cannot retrieve Tenant B file.

---

# 57. SEARCH LEAKAGE TEST

Use identical keywords.

Tenant A:
Ticket subject = "Card issue"

Tenant B:
Ticket subject = "Card issue"

Search as A returns only A.

---

# 58. AI/RAG LEAKAGE TEST

Tenant A document contains unique phrase:

"TAKAFUL-A-PRIVATE-789"

Tenant B user asks AI for it.

The answer/retrieval must not surface that phrase.

This should be a mandatory automated integration test where AI retrieval architecture permits.

---

# 59. CACHE LEAKAGE TEST

Cache dashboard for Tenant A.

Request same dashboard as Tenant B.

Ensure counts and records do not leak.

---

# 60. TENANT-SAFE OBJECT CREATION

Tenant must never be supplied as an editable normal form field for ordinary users.

When creating:

ticket = Ticket(
    tenant=request.tenant,
    ...
)

Do not use:

tenant = form.cleaned_data["tenant"]

unless this is an explicit platform administration operation.

---

# 61. DOMAIN SERVICE CONTRACT

Every tenant-aware business service should accept tenant/context explicitly.

Example:

create_ticket(
    *,
    tenant,
    actor,
    ...
)

Not:

create_ticket(actor, ...)

then infer tenant from arbitrary input.

This makes tenant ownership visible in code review.

---

# 62. EVENT CONTRACT

Any domain event should include:

tenant_id

Example:

TicketCreated:
    tenant_id
    ticket_id
    actor_id
    timestamp

This prevents async consumers from having to infer ownership.

---

# 63. TENANT SAFE REFERENCES

Ticket references may be unique per tenant.

Example:

Takaful Oman:
GLIS-2026-000001

Company B:
GLIS-2026-000001

This is acceptable because tenant provides namespace.

Internal UUID remains globally unique.

URL preferably uses:

tenant-safe opaque UUID/reference

with authorization.

Do not assume human reference is globally unique.

---

# 64. COMPANY-SPECIFIC SEQUENCES

ReferenceCounter:

- tenant
- prefix
- year
- current_value

Unique:

tenant + prefix + year

This allows each company to have its own numbering.

---

# 65. COMPANY-SPECIFIC EMAIL TEMPLATES

Tenant may customize:

- logo
- sender display name
- footer
- support contact
- ticket template
- notification content

Do not allow one tenant to edit platform/global template that changes all tenants.

Use:

platform default
→ tenant override

---

# 66. COMPANY-SPECIFIC WORKFLOW

Takaful Oman may configure:

Member Addition:
Intake
→ Validation
→ Approval
→ TPA
→ Complete

Company B may configure different rules.

Workflow definitions therefore belong to Tenant.

Code provides supported workflow capabilities.

Configuration determines tenant-specific behavior.

Do not let completely arbitrary workflow scripting execute unsafe Python.

---

# 67. COMPANY-SPECIFIC BUSINESS RULES

Configuration may vary by tenant:

- backdating days
- auto approval amount
- card requirement
- TPA requirement
- refund calculation
- approval levels
- SLA
- attachment requirements

Use structured tenant configuration.

Business rules requiring code should use named rule implementations referenced from configuration.

Avoid storing executable code in database.

---

# 68. COMPANY-SPECIFIC AI TRAINING

AI extraction prompts/examples belong to tenant.

Takaful Oman training examples must not be exposed to another tenant.

Support:

Platform Default Prompt

then:

Tenant Override

then:

Transaction-Type Override

Prompt resolution:

platform safe default
→ tenant configuration
→ purpose
→ transaction type
→ training examples

---

# 69. COMPANY-SPECIFIC KNOWLEDGE BASE

Knowledge article can be:

PLATFORM_PUBLIC_TO_TENANTS

or:

TENANT_PRIVATE

Tenant-private article must never appear in another tenant's search/RAG.

---

# 70. COMPANY-SPECIFIC DOCUMENT CENTER

Every document belongs to tenant unless explicitly platform-owned.

Platform documents:

- user guides
- generic manuals

Tenant documents:

- policies
- SOP
- claims material
- contracts
- customer documents

Keep scopes explicit.

---

# 71. COMPANY-SPECIFIC DASHBOARD CONFIGURATION

Allow tenant dashboard configuration:

- enabled KPI cards
- chart types
- visible modules
- default date range

But authorization controls which underlying data may appear.

Configuration cannot override data security.

---

# 72. PLATFORM SUPPORT ACCESS

If platform support needs to enter customer tenant:

require deliberate action.

Recommended:

"Access tenant as support"

Require:

- platform privilege
- reason
- optional ticket/reference
- short session duration
- prominent support-access banner
- complete audit

Avoid invisible permanent cross-tenant browsing.

---

# 73. SUPPORT IMPERSONATION

If impersonation is implemented:

record:

- actual platform user
- impersonated user
- tenant
- start time
- end time
- reason

Show persistent UI indicator.

Never hide impersonation from audit.

---

# 74. TENANT DELETE POLICY

Do not hard-delete customer tenant casually.

Support:

- active
- suspended
- archived

Actual deletion should require dedicated retention/data-destruction workflow.

Relationships should usually use PROTECT for security-sensitive tenant ownership.

---

# 75. DATA EXPORT / OFFBOARDING

Provide optional Tenant Export.

Can export tenant-owned:

- organizations
- users/memberships
- configuration
- tickets
- policies
- members
- audit
- documents metadata
- files where authorized

Export must not include other tenants.

This is useful for customer offboarding and disaster recovery.

---

# 76. BACKUP AND RESTORE

Platform backups may contain all tenants.

Operational restoration must be capable of understanding tenant ownership.

If tenant-level restore/export is required later, the explicit tenant key on every root table makes this feasible.

---

# 77. RECOMMENDED DATA MODEL

Conceptual hierarchy:

User
├── TenantMembership
│   └── Tenant
│       ├── Organization
│       ├── TenantGroup
│       ├── Project
│       │   ├── Product
│       │   │   └── Category
│       ├── Policy
│       │   ├── BenefitPlan
│       │   └── Member
│       ├── Ticket
│       │   ├── Comment
│       │   ├── Attachment
│       │   ├── Approval
│       │   ├── Task
│       │   └── Domain Transaction
│       ├── AIProviderConfig
│       ├── MailboxConfig
│       ├── KnowledgeArticle
│       ├── Document
│       ├── SLA
│       ├── Workflow
│       ├── Notification
│       └── AuditEvent
│
└── optional memberships in additional tenants

---

# 78. TAKAful OMAN EXAMPLE

Suppose the application hosts:

TENANT 1:
Takaful Oman

TENANT 2:
ABC Insurance

Nasir memberships:

Takaful Oman:
Administrator

ABC Insurance:
none

When Nasir logs in:

Active Tenant:
Takaful Oman

Nasir can see:

- Takaful Oman organizations
- Takaful Oman policies
- Takaful Oman members
- Takaful Oman tickets
- Takaful Oman tasks
- Takaful Oman TPA workflows
- Takaful Oman email intake
- Takaful Oman analytics
- Takaful Oman AI configuration

Nasir cannot see:

anything belonging to ABC Insurance.

Even if Nasir manually enters:

/portal/tickets/<ABC-ticket-id>/

return:

404/403 according to security policy.

Later platform administrator grants Nasir:

ABC Insurance
AUDITOR

Nasir now gets ABC Insurance in the company switcher.

When ABC Insurance is selected:

Nasir sees ABC data according to Auditor permissions only.

Nasir's Takaful Oman Administrator role does not follow him into ABC Insurance.

---

# 79. IMPLEMENTATION RULE

Treat `tenant_id` as important as a foreign key to the security perimeter.

When writing any new feature, the developer must ask:

1. Who owns this record?
2. What tenant does it belong to?
3. How is tenant determined?
4. Can a user forge tenant context?
5. Are foreign keys tenant-consistent?
6. Are queryset/list/search/export operations scoped?
7. Are files scoped?
8. Are caches scoped?
9. Are jobs scoped?
10. Are AI/vector operations scoped?
11. Are audit records scoped?
12. Is there a cross-tenant regression test?

A feature failing any of these questions is incomplete.

---

# 80. MOST IMPORTANT SECURITY PRINCIPLE

The application must behave as:

"one private GLIS installation per company"

even though multiple companies use the same deployed platform.

Cross-company access is a deliberately configured exception, never the default.

Tenant isolation must be enforced in:

DATABASE QUERY
+
APPLICATION SERVICE
+
AUTHORIZATION
+
FILE ACCESS
+
SEARCH
+
CACHE
+
ASYNC JOB
+
AI/RAG
+
API
+
ADMIN
+
EXPORT
+
AUDIT

Do not rely on hiding records in the frontend.
