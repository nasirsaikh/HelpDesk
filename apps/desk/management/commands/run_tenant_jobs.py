import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.desk.agents import expire_agent_runs, run_agent
from apps.desk.jobs import run_job
from apps.desk.models import AIAgentRun, Job


class Command(BaseCommand):
    help = "Run company-scoped queued jobs; --watch polls every 15 seconds."

    def add_arguments(self, parser):
        parser.add_argument("--company")
        parser.add_argument("--watch", action="store_true")

    def handle(self, **options):
        while True:
            expire_agent_runs(company_code=options["company"])
            # Deliberate platform dispatcher. Work happens only inside run_job's tenant context.
            jobs = Job.all_objects.filter(
                state="QUEUED",
                scheduled_at__lte=timezone.now(),
                tenant__active=True,
                tenant__status="ACTIVE",
            ).select_related("tenant")
            if options["company"]:
                jobs = jobs.filter(tenant__code=options["company"])
            for job in jobs[:100]:
                result = run_job(job_id=job.pk, tenant=job.tenant)
                self.stdout.write(
                    f"Company {job.tenant.code}: job {job.uuid} {'complete' if result else 'not completed'}"
                )
            agents = AIAgentRun.all_objects.filter(
                status="QUEUED", tenant__active=True, tenant__status="ACTIVE"
            ).select_related("tenant")
            if options["company"]:
                agents = agents.filter(tenant__code=options["company"])
            for run in agents.order_by("created_at")[:100]:
                result = run_agent(tenant=run.tenant, run_uuid=run.uuid)
                self.stdout.write(
                    f"Company {run.tenant.code}: agent run {run.uuid} {'complete' if result else 'not completed'}"
                )
            if not options["watch"]:
                break
            time.sleep(15)
