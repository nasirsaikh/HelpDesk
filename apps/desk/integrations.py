"""Microsoft Graph mailbox operations always begin at a tenant-owned mailbox."""

import json
import os
import re
from html.parser import HTMLParser
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.utils import timezone

from apps.tenancy.context import current_context

from .ai import extract_email
from .models import EmailAuthority, InboundEmail, Policy


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def resolve_email_authority(*, tenant, sender, policy, transaction_type, as_of=None):
    ctx = current_context()
    if not ctx or ctx.tenant_id != tenant.pk or policy.tenant_id != tenant.pk:
        raise PermissionDenied("Mailbox company mismatch.")
    as_of = as_of or timezone.localdate()
    authorities = (
        EmailAuthority.objects.filter(
            email_address__iexact=sender.strip(),
            active=True,
            organization_id=policy.organization_id,
        )
        .filter(Q(policy=policy) | Q(policy__isnull=True))
        .filter(
            Q(valid_from__isnull=True) | Q(valid_from__lte=as_of),
            Q(valid_until__isnull=True) | Q(valid_until__gte=as_of),
        )
    )
    return next(
        (row for row in authorities if transaction_type in row.permitted_transaction_types), None
    )


def process_inbound(*, email):
    ctx = current_context()
    if not ctx or email.tenant_id != ctx.tenant_id:
        raise PermissionDenied("Inbound email company mismatch.")
    payload = extract_email(email) if ctx.tenant.feature_flags.get("ai", True) else {}
    if not payload:
        # Deterministic fallback retains the email for manual review; it never fabricates members.
        match = re.search(r"\bP[/_][A-Za-z0-9/_-]+", email.subject + " " + email.body)
        text = (email.subject + " " + email.body).lower()
        kind = (
            "MEMBER_ADD"
            if any(word in text for word in ["addition", "add member"])
            else "MEMBER_DELETE"
            if any(word in text for word in ["deletion", "delete member"])
            else ""
        )
        payload = {
            "policy_number": match.group().replace("_", "/") if match else None,
            "transaction_type": kind,
            "members": [],
        }
    email.extracted_payload = payload
    policy = Policy.objects.filter(policy_number=payload.get("policy_number")).first()
    email.policy = policy
    if (
        policy
        and payload.get("transaction_type")
        and not resolve_email_authority(
            tenant=ctx.tenant,
            sender=email.sender,
            policy=policy,
            transaction_type=payload["transaction_type"],
            as_of=email.received_at.date(),
        )
    ):
        email.state = "UNAUTHORIZED"
    else:
        email.state = "NEEDS_REVIEW"
    email.save()
    return email


def graph_json(url, *, token=None, data=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    with urlopen(Request(url, headers=headers, data=data), timeout=60) as response:
        return json.loads(response.read(8 * 1024 * 1024))


def sync_mailbox(*, mailbox):
    ctx = current_context()
    if (
        not ctx
        or mailbox.tenant_id != ctx.tenant_id
        or not ctx.tenant.available
        or not mailbox.active
    ):
        raise PermissionDenied("Mailbox is not active in this company.")
    if not ctx.tenant.feature_flags.get("mailbox", True):
        raise PermissionDenied("Mailbox processing is disabled for this company.")
    secret = os.environ.get(mailbox.secret_env, "")
    if not secret or not re.fullmatch(r"[a-zA-Z0-9-]+", mailbox.graph_directory_id):
        raise ValidationError(
            "Graph directory, client ID and secret environment reference must be configured."
        )
    token_data = graph_json(
        f"https://login.microsoftonline.com/{mailbox.graph_directory_id}/oauth2/v2.0/token",
        data=urlencode(
            {
                "client_id": mailbox.graph_client_id,
                "client_secret": secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            }
        ).encode(),
    )
    token = token_data["access_token"]
    base_path = (
        f"/v1.0/users/{quote(mailbox.email_address, safe='')}/mailFolders/inbox/messages/delta"
    )
    url = (
        mailbox.cursor
        or "https://graph.microsoft.com"
        + base_path
        + "?$select=id,subject,from,body,receivedDateTime,internetMessageId"
    )
    processed = 0
    for _ in range(5):
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.hostname != "graph.microsoft.com"
            or parts.path != base_path
        ):
            raise ValidationError(
                "Mailbox cursor must target this mailbox's Graph inbox delta endpoint."
            )
        result = graph_json(url, token=token)
        for message in result.get("value", []):
            if "@removed" in message:
                continue
            sender = message.get("from", {}).get("emailAddress", {}).get("address", "")
            if not sender:
                continue
            body = message.get("body", {}).get("content", "")
            if message.get("body", {}).get("contentType", "").lower() == "html":
                parser = TextParser()
                parser.feed(body)
                body = " ".join(parser.parts)
            email, created = InboundEmail.objects.get_or_create(
                mailbox=mailbox,
                message_id=message["id"],
                defaults={
                    "sender": sender.lower(),
                    "subject": message.get("subject", "")[:255] or "(No subject)",
                    "body": body,
                    "received_at": message.get("receivedDateTime") or timezone.now(),
                },
            )
            if created:
                process_inbound(email=email)
                processed += 1
        url = result.get("@odata.nextLink") or result.get("@odata.deltaLink")
        if url:
            mailbox.cursor = url
            mailbox.save(update_fields=["cursor", "updated_at"])
        if not result.get("@odata.nextLink"):
            break
    return processed
