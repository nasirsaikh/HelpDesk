from django.core.exceptions import ValidationError


def secret_prefix(tenant):
    return f"TENANT_{tenant.uuid.hex.upper()}_"


def validate_secret_reference(reference, tenant):
    """A company administrator cannot select another company's process secret."""
    import re

    if reference and (
        not re.fullmatch(r"[A-Z_][A-Z0-9_]{2,79}", reference)
        or not reference.startswith(secret_prefix(tenant))
    ):
        raise ValidationError(
            f"Use an environment variable name beginning with {secret_prefix(tenant)}. Secret values belong in the worker environment."
        )
