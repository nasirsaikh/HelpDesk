# API usage

Create a service account under **Company settings → Service accounts**, assign explicit scopes and copy its token once. Only the SHA-256 hash is stored. Set active=false or an expiry to revoke it.

```http
GET /api/tickets/
Authorization: Bearer hd_<copied-token>
```

The credential resolves its owning company. A normal service account cannot select a different company using X-Tenant-ID. A platform-approved service-account grant can explicitly allow a target company; in that case only target grant capabilities apply and the source token's capabilities do not transfer.

| Endpoint | Method | Required scope | Result |
|---|---|---|---|
| `/api/tickets/` | GET | ticket.view | Up to 100 scoped tickets; optional q filter |
| `/api/tickets/<uuid>/` | GET | ticket.view | One scoped ticket; foreign UUID returns 404 |
| `/api/policies/` | GET | policy.view | Up to 100 scoped policies |
| `/api/policies/<uuid>/` | GET | policy.view | One scoped policy |
| `/api/knowledge/?q=...` | GET | knowledge.view | Current-company published retrieval context |
| `/api/analytics/` | POST | report.view plus dataset capabilities | Bounded read-only SQL over authorized snapshot tables |

API reads also support authenticated session users and their verified active company. Session users submit analytics through the CSRF-protected portal. API analytics POST requires a bearer credential, so cookie-only requests cannot bypass CSRF.

```http
POST /api/analytics/
Authorization: Bearer hd_<copied-token>
Content-Type: application/json

{"sql": "SELECT status, COUNT(*) AS total FROM tickets GROUP BY status"}
```

Grant ticket.view to make ticket data available to a report service account; report.view alone does not silently grant access to all datasets. Snapshot tables contain only allowlisted columns. SELECT against desk_ticket, schema tables, extension functions, attached databases, mutations and multi-statements is rejected. Requests are bounded to 10,000 SQL characters, two seconds of query execution and 500 result rows; snapshots contain at most 50,000 authorized rows per table.

Invalid credentials return 401, invalid company/scope returns 403, unavailable modules or inaccessible objects return 404, and malformed data returns 400. Tokens should be stored outside source control and sent only over HTTPS.
