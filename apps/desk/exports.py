import csv
import io

from django.core.exceptions import ValidationError

from .ai import report_queryset
from .models import Ticket


def csv_cell(value):
    value = "" if value is None else str(value)
    # Neutralize spreadsheet formulas in exported user-provided text.
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else value


def ticket_report(*, filters=None):
    filters = filters or {}
    if set(filters) - {"status", "priority"}:
        raise ValidationError("Unsupported report filter.")
    rows = (
        report_queryset(Ticket)
        .filter(**filters)
        .values_list("reference", "title", "status", "priority", "workflow_stage", "created_at")
    )
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Reference", "Title", "Status", "Priority", "Stage", "Created"])
    for row in rows:
        writer.writerow([csv_cell(value) for value in row])
    return out.getvalue().encode("utf-8-sig")
