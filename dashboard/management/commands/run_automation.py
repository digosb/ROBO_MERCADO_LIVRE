from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.events import EVENT_JOB_ERROR
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from dashboard.automation import (
    AutomationProcessLock,
    AutomationRuntime,
    record_scheduler_error,
)
from dashboard.models import Configuracao


class Command(BaseCommand):
    help = "Executa o motor APScheduler independente das requisições web."

    def handle(self, *args, **options):
        Configuracao.current()
        process_lock = AutomationProcessLock()
        if not process_lock.acquire():
            raise CommandError("Já existe um processo do motor ativo para este banco.")

        scheduler = None
        runtime = None
        try:
            scheduler = BlockingScheduler(timezone=timezone.get_current_timezone())
            runtime = AutomationRuntime(scheduler)
            scheduler.add_job(
                runtime.sync_control,
                trigger="interval",
                seconds=1,
                id="robot_control_poll",
                coalesce=True,
                max_instances=1,
                misfire_grace_time=1,
            )
            scheduler.add_job(
                runtime.heartbeat,
                trigger="interval",
                seconds=5,
                id="robot_worker_heartbeat",
                coalesce=True,
                max_instances=1,
                misfire_grace_time=2,
            )
            scheduler.add_listener(record_scheduler_error, EVENT_JOB_ERROR)
            runtime.heartbeat()
            self.stdout.write(self.style.SUCCESS("Motor APScheduler iniciado."))
            scheduler.start()
        except KeyboardInterrupt:
            self.stdout.write("Encerrando o motor com segurança...")
        finally:
            try:
                if scheduler and scheduler.running:
                    scheduler.shutdown(wait=True)
                if runtime:
                    runtime.mark_worker_stopped()
            finally:
                process_lock.release()
