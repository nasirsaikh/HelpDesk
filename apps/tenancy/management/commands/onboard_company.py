from django.core.management.base import BaseCommand

from apps.tenancy.services import onboard_company


class Command(BaseCommand):
    help = "Idempotently onboard a company and its administrator; does not send email."

    def add_arguments(self, parser):
        parser.add_argument("--code", required=True)
        parser.add_argument("--name", required=True)
        parser.add_argument("--admin-email", required=True)

    def handle(self, **options):
        tenant, user, _ = onboard_company(
            code=options["code"], name=options["name"], admin_email=options["admin_email"]
        )
        self.stdout.write(
            self.style.SUCCESS(f"Company {tenant.code} ready. Administrator: {user.username}")
        )
        if not user.has_usable_password():
            self.stdout.write(
                f"Set a password with: python manage.py changepassword {user.username}"
            )
