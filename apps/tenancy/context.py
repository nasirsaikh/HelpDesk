from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TenantContext:
    tenant: object
    actor: object = None
    role: str = ""
    grants: tuple = field(default_factory=tuple)
    service_account: object = None
    system: bool = False
    support: bool = False

    @property
    def tenant_id(self):
        return self.tenant.pk


_context = ContextVar("helpdesk_tenant_context", default=None)


def current_context():
    return _context.get()


@contextmanager
def tenant_context(
    tenant, *, actor=None, role="", grants=(), service_account=None, system=False, support=False
):
    """Trusted boundary. Request/worker entry points must validate authorization first."""
    token = _context.set(
        TenantContext(tenant, actor, role, tuple(grants), service_account, system, support)
    )
    try:
        yield _context.get()
    finally:
        _context.reset(token)
