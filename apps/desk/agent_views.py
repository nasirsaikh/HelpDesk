from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .agents import (
    agent_ready,
    own_agent_runs,
    queue_agent_runs,
    review_agent_action,
    visible_agents,
)
from .views import protected


def history_context(request):
    runs = list(own_agent_runs(tenant=request.tenant, actor=request.user)[:20])
    return {"runs": runs, "poll_runs": any(r.status in {"QUEUED", "RUNNING"} for r in runs)}


@protected(feature="ai")
def assistant(request):
    if not request.tenant.feature_flags.get("ai_agents", True):
        from django.http import Http404

        raise Http404
    error = ""
    query, selection = "", "AUTO"
    if request.method == "POST":
        query, selection = request.POST.get("query", ""), request.POST.get("agent", "AUTO")
        try:
            runs = queue_agent_runs(
                tenant=request.tenant, actor=request.user, selection=selection, query=query
            )
            messages.success(
                request, f"Queued {len(runs)} agent request(s). Results will appear below."
            )
            return redirect("desk:agent-assistant")
        except ValidationError as exc:
            error = "; ".join(exc.messages)
    agents = visible_agents(tenant=request.tenant)
    return render(
        request,
        "desk/agent_assistant.html",
        {
            "agents": [{"config": a, "ready": agent_ready(a)} for a in agents],
            **history_context(request),
            "query": query,
            "selection": selection,
            "error": error,
        },
    )


@protected(feature="ai")
def history(request):
    if not request.tenant.feature_flags.get("ai_agents", True):
        from django.http import Http404

        raise Http404
    return render(
        request,
        "partials/agent_runs.html",
        history_context(request),
    )


@protected(feature="ai")
def detail(request, uuid):
    if not request.tenant.feature_flags.get("ai_agents", True):
        from django.http import Http404

        raise Http404
    run = get_object_or_404(
        own_agent_runs(tenant=request.tenant, actor=request.user, uuid=uuid), uuid=uuid
    )
    return render(request, "desk/agent_run.html", {"run": run})


@protected(feature="ai")
@require_POST
def review(request, uuid):
    if not request.tenant.feature_flags.get("ai_agents", True):
        from django.http import Http404

        raise Http404
    try:
        action = review_agent_action(
            tenant=request.tenant,
            actor=request.user,
            action_uuid=uuid,
            decision=request.POST.get("decision"),
        )
        messages.success(request, "Draft posted." if action.applied_at else "Draft dismissed.")
        return redirect("desk:agent-run", uuid=action.run.uuid)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect("desk:agent-assistant")
