from django.urls import path

from . import agent_views, api, views

app_name = "desk"
urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("tickets/", views.ticket_list, name="ticket-list"),
    path("tickets/new/", views.ticket_create, name="ticket-create"),
    path("tickets/<uuid:uuid>/", views.ticket_detail, name="ticket-detail"),
    path("tickets/<uuid:uuid>/approval/", views.approval_create, name="approval-create"),
    path("tickets/<uuid:uuid>/attach/", views.attachment_upload, name="attachment-upload"),
    path("approvals/<uuid:uuid>/decide/", views.approval_decide, name="approval-decide"),
    path("files/<str:kind>/<uuid:uuid>/", views.download, name="download"),
    path("policies/<uuid:uuid>/", views.policy_detail, name="policy-detail"),
    path(
        "policies/<uuid:uuid>/<str:child>/new/", views.policy_child_form, name="policy-child-create"
    ),
    path(
        "policies/<uuid:uuid>/member/<uuid:member_uuid>/",
        views.policy_child_form,
        {"child": "member"},
        name="member-edit",
    ),
    path(
        "transactions/<uuid:uuid>/complete/",
        views.transaction_complete,
        name="transaction-complete",
    ),
    path("knowledge/<uuid:uuid>/", views.knowledge_detail, name="knowledge-detail"),
    path("notifications/", views.notifications, name="notifications"),
    path("search/", views.search, name="search"),
    path("reports/", views.reports, name="reports"),
    path("reports/tickets.csv", views.export_tickets, name="export-tickets"),
    path("reports/queue/", views.export_queue, name="export-queue"),
    path("reports/<uuid:uuid>/download/", views.export_download, name="export-download"),
    path("ai/search/", views.ai_search, name="ai-search"),
    path("ai/agents/", agent_views.assistant, name="agent-assistant"),
    path("ai/agents/history/", agent_views.history, name="agent-history"),
    path("ai/agents/runs/<uuid:uuid>/", agent_views.detail, name="agent-run"),
    path("ai/agents/drafts/<uuid:uuid>/review/", agent_views.review, name="agent-review"),
    path("settings/", views.settings, name="settings"),
    path("settings/invite/", views.invite, name="invite"),
    path("settings/members/<int:pk>/", views.membership_edit, name="membership-edit"),
    path("settings/mailbox/<uuid:uuid>/sync/", views.mailbox_queue, name="mailbox-queue"),
    path("settings/<str:kind>/", views.config_list, name="config-list"),
    path("settings/<str:kind>/new/", views.config_edit, name="config-create"),
    path("settings/<str:kind>/<uuid:uuid>/", views.config_edit, name="config-edit"),
    path("api/tickets/", api.tickets, name="api-tickets"),
    path("api/tickets/<uuid:uuid>/", api.tickets, name="api-ticket"),
    path("api/policies/", api.policies, name="api-policies"),
    path("api/policies/<uuid:uuid>/", api.policies, name="api-policy"),
    path("api/analytics/", api.analytics, name="api-analytics"),
    path("api/knowledge/", api.knowledge, name="api-knowledge"),
]
for kind in views.REGISTRY:
    urlpatterns += [
        path(f"{kind}/", views.catalog_list, {"kind": kind}, name=f"{kind}-list"),
        path(f"{kind}/new/", views.catalog_form, {"kind": kind}, name=f"{kind}-create"),
        path(f"{kind}/<uuid:uuid>/edit/", views.catalog_form, {"kind": kind}, name=f"{kind}-edit"),
    ]
# Human-friendly aliases keep route names stable across the portal.
urlpatterns += [
    path("policies/", views.catalog_list, {"kind": "policy"}, name="policy-list"),
    path("organizations/", views.catalog_list, {"kind": "organization"}, name="organization-list"),
    path("tasks/", views.catalog_list, {"kind": "task"}, name="task-list"),
    path("transactions/", views.catalog_list, {"kind": "transaction"}, name="transaction-list"),
    path("documents/", views.catalog_list, {"kind": "document"}, name="document-list"),
]
