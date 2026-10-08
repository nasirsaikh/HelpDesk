import hashlib
import json

from django.core.exceptions import PermissionDenied

from .context import current_context


def tenant_cache_key(name, *, extra=""):
    ctx = current_context()
    if not ctx:
        raise PermissionDenied("Company context required for cache access.")
    scope = {
        "user": getattr(ctx.actor, "pk", None),
        "role": ctx.role,
        "grants": [(g.pk, g.scopes, g.resource_scope, str(g.valid_until)) for g in ctx.grants],
        "service": getattr(ctx.service_account, "pk", None),
        "support": ctx.support,
    }
    digest = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()[:20]
    return f"tenant:{ctx.tenant.uuid}:{digest}:{name}:{extra}"


def access_signature():
    ctx = current_context()
    if not ctx:
        raise PermissionDenied("Company context required.")
    return json.dumps(
        {"role": ctx.role, "grants": [(g.pk, g.scopes, g.resource_scope) for g in ctx.grants]},
        sort_keys=True,
    )
