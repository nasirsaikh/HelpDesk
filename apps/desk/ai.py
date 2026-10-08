"""Retrieval is scoped before ranking; analytics SQL sees only an authorized in-memory snapshot."""

import json
import math
import os
import re
import sqlite3
import time
from urllib.request import Request, urlopen

from django.core.exceptions import PermissionDenied, ValidationError

from apps.tenancy.access import authorization_q, grant_allows, require_capability, restriction_q
from apps.tenancy.context import current_context

from . import models as m
from .services import authorized_service

SAFE_DEFAULT_PROMPT = "Extract facts only from the provided source. Return a JSON object with policy_number, transaction_type, effective_date, members, missing_information. Never invent missing values. Members must be an array of objects."


def retrieve_knowledge_context(*, query):
    ctx = current_context()
    if not ctx:
        raise PermissionDenied
    return retrieve_knowledge(tenant=ctx.tenant, actor=ctx.actor, query=query)


def execute_analytics_context(*, sql):
    ctx = current_context()
    if not ctx:
        raise PermissionDenied
    return execute_analytics(tenant=ctx.tenant, actor=ctx.actor, sql=sql)


def resolve_prompt(purpose="EXTRACTION", transaction_type=""):
    ctx = current_context()
    if not ctx:
        raise PermissionDenied("Company context is required.")
    # Providers/prompts are deliberately read by reviewed internal code, after context validation.
    prompts = m.AIPrompt.all_objects.filter(tenant_id=ctx.tenant_id, purpose=purpose)
    generic = prompts.filter(transaction_type="").first()
    specific = (
        prompts.filter(transaction_type=transaction_type).first() if transaction_type else None
    )
    layers = [SAFE_DEFAULT_PROMPT]
    for prompt in [generic, specific]:
        if prompt:
            layers += [prompt.prompt, json.dumps(prompt.training_examples)]
    return "\n\n".join(layers)


def retrieve_knowledge(*, tenant, actor, query, embedding=None, limit=5):
    with authorized_service(tenant=tenant, actor=actor):
        require_capability("knowledge")
        words = set(re.findall(r"\w+", query.lower()))
        candidates = m.VectorDocument.objects.select_related("article").filter(
            article__published=True
        )
        scored = []
        for document in candidates[:2000]:
            if document.namespace != f"tenant_{tenant.uuid.hex}":
                continue
            if embedding is not None and len(embedding) == len(document.embedding) and embedding:
                numerator = sum(a * b for a, b in zip(embedding, document.embedding))
                denominator = math.sqrt(
                    sum(a * a for a in embedding) * sum(b * b for b in document.embedding)
                )
                score = numerator / denominator if denominator else 0
            else:
                score = len(words & set(re.findall(r"\w+", document.content.lower())))
            if score > 0:
                scored.append((score, document))
        return [
            {
                "article_uuid": str(doc.article.uuid),
                "title": doc.article.title,
                "content": doc.content,
                "tenant_id": tenant.pk,
                "namespace": doc.namespace,
            }
            for _, doc in sorted(scored, key=lambda item: item[0], reverse=True)[:limit]
        ]


def provider_completion(*, provider, messages, sensitive=True, timeout_seconds=None):
    ctx = current_context()
    if (
        not ctx
        or provider.tenant_id != ctx.tenant_id
        or not provider.active
        or not ctx.tenant.feature_flags.get("ai", True)
    ):
        raise PermissionDenied("AI provider is unavailable in this company.")
    if sensitive and not provider.allow_sensitive_data:
        raise PermissionDenied("This provider has not been enabled for company-sensitive data.")
    headers = {"Content-Type": "application/json"}
    token = os.environ.get(provider.token_env, "") if provider.token_env else ""
    if provider.token_env and not token:
        raise ValidationError("The AI token environment variable is not configured.")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if provider.provider == "OLLAMA":
        url = provider.endpoint.rstrip("/") + "/api/chat"
        payload = {"model": provider.model, "messages": messages, "stream": False, "format": "json"}
    else:
        url = provider.endpoint.rstrip("/") + "/chat/completions"
        payload = {
            "model": provider.model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
    with urlopen(
        Request(url, data=json.dumps(payload).encode(), headers=headers),
        timeout=min(provider.timeout_seconds, timeout_seconds)
        if timeout_seconds is not None
        else provider.timeout_seconds,
    ) as response:
        data = json.loads(response.read(2 * 1024 * 1024))
    return (
        data["message"]["content"]
        if provider.provider == "OLLAMA"
        else data["choices"][0]["message"]["content"]
    )


def email_domain(transaction_type):
    if transaction_type == "CLAIM":
        return "CLAIMS"
    return (
        "POLICY"
        if transaction_type
        in {"MEMBER_ADD", "MEMBER_DELETE", "TERMINATION", "DEMOGRAPHIC_CHANGE", "POLICY_ENROLL"}
        else "TICKETING"
    )


def email_provider(domain):
    from .agent_specs import DOMAIN_FEATURES

    ctx = current_context()
    if not all(ctx.tenant.feature_flags.get(flag, True) for flag in DOMAIN_FEATURES[domain]):
        return None
    agent = (
        m.AIAgentConfig.all_objects.filter(tenant_id=ctx.tenant_id, domain=domain, is_default=True)
        .select_related("provider")
        .first()
    )
    if agent and not agent.active:
        raise ValidationError("The default email agent is inactive.")
    if agent and agent.provider_id:
        return agent.provider
    # Backward compatibility for a single provider. Multiple providers require an explicit binding.
    providers = list(
        m.AIProviderConfig.all_objects.filter(tenant_id=ctx.tenant_id, active=True)[:2]
    )
    if len(providers) > 1:
        raise ValidationError(
            "Choose a provider for the default agent in this email's business area."
        )
    return providers[0] if providers else None


def extraction_payload(raw):
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        raise ValidationError("AI returned invalid JSON.")
    required = {"policy_number", "transaction_type", "members"}
    if (
        not isinstance(payload, dict)
        or not required <= payload.keys()
        or not isinstance(payload["members"], list)
        or not all(isinstance(row, dict) for row in payload["members"])
    ):
        raise ValidationError("AI response does not match the required extraction schema.")
    if not isinstance(payload["policy_number"], (str, type(None))) or not isinstance(
        payload["transaction_type"], (str, type(None))
    ):
        raise ValidationError("AI policy and transaction type must be text or null.")
    return {
        key: payload[key]
        for key in [
            "policy_number",
            "transaction_type",
            "effective_date",
            "members",
            "missing_information",
        ]
        if key in payload
    }


def extract_email(email):
    ctx = current_context()
    if not ctx or email.tenant_id != ctx.tenant_id:
        raise PermissionDenied("Email company mismatch.")
    words = (email.subject + " " + email.body).lower()
    hints = [
        ("CLAIM", r"\bclaims?\b"),
        ("MEMBER_DELETE", r"\b(deletion|delete member)\b"),
        ("MEMBER_ADD", r"\b(addition|add member)\b"),
        ("TERMINATION", r"\btermination\b"),
        ("POLICY_ENROLL", r"\b(enrollment|enrolment)\b"),
        ("DEMOGRAPHIC_CHANGE", r"\bdemographic\b"),
    ]
    hint = next((kind for kind, pattern in hints if re.search(pattern, words)), "")
    provider = email_provider(email_domain(hint))
    if not provider:
        return {}
    raw = provider_completion(
        provider=provider,
        messages=[
            {"role": "system", "content": resolve_prompt(transaction_type=hint)},
            {"role": "user", "content": f"Subject: {email.subject}\n{email.body}"},
        ],
    )
    payload = extraction_payload(raw)
    kind = payload.get("transaction_type") or ""
    specific = (
        m.AIPrompt.all_objects.filter(
            tenant_id=ctx.tenant_id, purpose="EXTRACTION", transaction_type=kind
        ).exists()
        if kind
        else False
    )
    if (
        kind != hint
        and kind in dict(m.Transaction._meta.get_field("transaction_type").choices)
        and (specific or email_domain(kind) != email_domain(hint))
    ):
        provider = email_provider(email_domain(kind))
        if not provider:
            return {}
        payload = extraction_payload(
            provider_completion(
                provider=provider,
                messages=[
                    {"role": "system", "content": resolve_prompt(transaction_type=kind)},
                    {
                        "role": "user",
                        "content": f"Extract this {kind} request. Subject: {email.subject}\n{email.body}",
                    },
                ],
            )
        )
        if payload.get("transaction_type") != kind:
            raise ValidationError(
                "Transaction classification changed during extraction. Review the email."
            )
    return payload


def report_queryset(model):
    require_capability("report")
    ctx = current_context()
    allowed = authorization_q(model, ctx)
    for grant in ctx.grants:
        if grant_allows(grant, "report", "view"):
            allowed |= restriction_q(model, grant)
    return model.all_objects.filter(tenant_id=ctx.tenant_id).filter(allowed)


def execute_analytics(*, tenant, actor, sql):
    with authorized_service(tenant=tenant, actor=actor):
        require_capability("report")
        if len(sql) > 10000 or not re.match(r"^\s*(SELECT|WITH)\b", sql, re.I):
            raise ValidationError("Only bounded read-only SELECT queries are accepted.")
        with sqlite3.connect(":memory:") as connection:
            datasets = {
                "tickets": (
                    m.Ticket,
                    ["reference", "title", "status", "priority", "workflow_stage"],
                ),
                "policies": (m.Policy, ["policy_number", "status"]),
                "members": (m.Member, ["member_id", "relationship", "active"]),
            }
            for table, (model, fields) in datasets.items():
                connection.execute(
                    f"CREATE TABLE {table} ("
                    + ",".join(f'"{field}" TEXT' for field in fields)
                    + ")"
                )
                rows = report_queryset(model).values_list(*fields)[:50000]
                connection.executemany(
                    f"INSERT INTO {table} VALUES (" + ",".join("?" for _ in fields) + ")", rows
                )
            connection.commit()
            functions = {
                "count",
                "sum",
                "avg",
                "min",
                "max",
                "coalesce",
                "round",
                "lower",
                "upper",
                "length",
                "substr",
                "abs",
            }

            def authorize(action, arg1, arg2, db, source):
                if action == sqlite3.SQLITE_SELECT:
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_READ and arg1 in datasets and db in {None, "main"}:
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in functions:
                    return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY

            connection.set_authorizer(authorize)
            started = time.monotonic()
            connection.set_progress_handler(lambda: int(time.monotonic() - started > 2), 1000)
            try:
                cursor = connection.execute(sql)
                return {
                    "columns": [column[0] for column in cursor.description],
                    "rows": cursor.fetchmany(500),
                    "max_rows": 500,
                }
            except sqlite3.DatabaseError:
                raise ValidationError(
                    "Query rejected. Use only the authorized tickets, policies and members snapshot tables."
                )
