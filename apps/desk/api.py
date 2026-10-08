import json

from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt

from apps.tenancy.access import require_capability

from . import models as m
from .ai import retrieve_knowledge


def api_error_handler(view):
    def wrapper(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except PermissionDenied:
            return JsonResponse({"error": "Operation denied in the active company."}, status=403)
        except Http404:
            return JsonResponse({"error": "Resource not found."}, status=404)
        except (ValidationError, ValueError, TypeError, KeyError):
            return JsonResponse({"error": "Invalid request data."}, status=400)

    return wrapper


def check_module(request, feature):
    if not request.tenant.feature_flags.get(feature, True):
        raise Http404


def ticket_data(obj):
    return {
        "uuid": str(obj.uuid),
        "reference": obj.reference,
        "title": obj.title,
        "status": obj.status,
        "priority": obj.priority,
        "workflow_stage": obj.workflow_stage,
    }


@api_error_handler
def tickets(request, uuid=None):
    check_module(request, "tickets")
    require_capability("ticket")
    if request.method != "GET":
        return JsonResponse({"error": "This endpoint accepts GET."}, status=405)
    if uuid:
        return JsonResponse(ticket_data(get_object_or_404(m.Ticket.objects, uuid=uuid)))
    qs = m.Ticket.objects.all()
    if request.GET.get("q"):
        qs = qs.filter(title__icontains=request.GET["q"][:200])
    return JsonResponse(
        {
            "company_uuid": str(request.tenant.uuid),
            "results": [ticket_data(obj) for obj in qs[:100]],
        }
    )


@api_error_handler
def policies(request, uuid=None):
    check_module(request, "policies")
    require_capability("policy")
    if request.method != "GET":
        return JsonResponse({"error": "This endpoint accepts GET."}, status=405)
    qs = m.Policy.objects.all()
    rows = [get_object_or_404(qs, uuid=uuid)] if uuid else qs[:100]
    return JsonResponse(
        {
            "results": [
                {"uuid": str(row.uuid), "policy_number": row.policy_number, "status": row.status}
                for row in rows
            ]
        }
    )


@csrf_exempt
@api_error_handler
def analytics(request):
    check_module(request, "analytics")
    require_capability("report")
    if request.method != "POST":
        return JsonResponse({"error": "Use POST with a scoped service credential."}, status=405)
    # Cookie-authenticated users use the CSRF-protected portal report form.
    if not getattr(request, "service_account", None) and not getattr(
        request, "external_service_account", None
    ):
        raise PermissionDenied
    payload = json.loads(request.body)
    # Execute through the already validated token context, never through a global actor role.
    from .ai import execute_analytics_context

    return JsonResponse(execute_analytics_context(sql=payload["sql"]))


@api_error_handler
def knowledge(request):
    check_module(request, "ai")
    require_capability("knowledge")
    if request.method != "GET":
        return JsonResponse({"error": "Use GET."}, status=405)
    if not request.user.is_authenticated:
        from .ai import retrieve_knowledge_context

        results = retrieve_knowledge_context(query=request.GET.get("q", "")[:1000])
    else:
        results = retrieve_knowledge(
            tenant=request.tenant, actor=request.user, query=request.GET.get("q", "")[:1000]
        )
    return JsonResponse({"results": results})
