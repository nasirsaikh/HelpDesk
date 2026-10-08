from django.core.management.base import BaseCommand

from apps.desk.integrations import sync_mailbox
from apps.desk.models import MailboxConfig
from apps.tenancy.context import tenant_context
from apps.tenancy.models import Tenant


class Command(BaseCommand):
    help = "Poll active Microsoft Graph mailboxes separately for each company. Schedule this command every minute."

    def add_arguments(self, parser):
        parser.add_argument("--company")

    def handle(self, **options):
        companies = Tenant.objects.filter(active=True, status="ACTIVE")
        if options["company"]:
            companies = companies.filter(code=options["company"])
        for tenant in companies:
            with tenant_context(tenant, system=True):
                for mailbox in MailboxConfig.objects.filter(active=True):
                    try:
                        count = sync_mailbox(mailbox=mailbox)
                        self.stdout.write(f"Company {tenant.code}: {count} emails imported")
                    except Exception as exc:
                        self.stderr.write(
                            f"Company {tenant.code}: mailbox {mailbox.uuid} failed ({type(exc).__name__})"
                        )
