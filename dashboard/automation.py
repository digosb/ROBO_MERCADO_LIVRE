import hashlib
import os
import tempfile
import threading
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.utils import timezone

from .mercadolivre import OfferDiscoveryError, OfferDiscoveryRateLimited, discover_flash_deals
from .models import (
    Configuracao,
    Envio,
    LinkAfiliado,
    LogSistema,
    MensagemWhatsApp,
    Oferta,
    Produto,
)
from .whatsapp import (
    WhatsAppDeliveryUnavailable,
    WhatsAppPreparationError,
    get_delivery_provider,
    is_official_delivery_ready,
    prepare_offer_message,
)


WORKER_HEARTBEAT_TIMEOUT = timedelta(seconds=15)
CYCLE_JOB_ID = "robot_offer_cycle"
MANUAL_JOB_ID = "robot_manual_cycle"


def is_worker_online(configuration=None):
    configuration = configuration or Configuracao.current()
    return bool(
        configuration.worker_heartbeat
        and timezone.now() - configuration.worker_heartbeat <= WORKER_HEARTBEAT_TIMEOUT
    )


def is_within_operating_hours(configuration, moment=None):
    if not configuration.operating_hours_enabled:
        return True
    local_time = timezone.localtime(moment or timezone.now()).time().replace(tzinfo=None)
    start = configuration.operating_start_time
    end = configuration.operating_end_time
    if start < end:
        return start <= local_time < end
    return local_time >= start or local_time < end


class AutomationProcessLock:
    """Garante um único processo APScheduler por banco de dados."""

    def __init__(self):
        database_name = str(settings.DATABASES["default"]["NAME"])
        key = hashlib.sha256(database_name.encode("utf-8")).hexdigest()[:20]
        self.path = Path(tempfile.gettempdir()) / f"robo-mercado-livre-{key}.lock"
        self.handle = None

    def acquire(self):
        self.handle = self.path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"\0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            self.handle.close()
            self.handle = None
            return False
        return True

    def release(self):
        if self.handle is None:
            return
        if os.name == "nt":
            import msvcrt

            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None


class AutomationRuntime:
    def __init__(self, scheduler, discovery_provider=None, delivery_provider=None):
        self.scheduler = scheduler
        self.discovery_provider = discovery_provider
        self.delivery_provider = delivery_provider or get_delivery_provider()
        self._cycle_lock = threading.Lock()
        self._schedule_signature = None

    def heartbeat(self):
        now = timezone.now()
        Configuracao.objects.filter(pk=1).update(worker_heartbeat=now, updated_at=now)

    def mark_worker_stopped(self):
        now = timezone.now()
        Configuracao.objects.filter(pk=1).update(
            worker_heartbeat=None,
            next_execution=None,
            updated_at=now,
        )

    def sync_control(self):
        configuration = Configuracao.current()
        schedule_signature = (
            configuration.status,
            configuration.send_interval_minutes,
            configuration.control_changed_at,
        )
        if configuration.status == Configuracao.Status.RUNNING:
            job = self.scheduler.get_job(CYCLE_JOB_ID)
            if job is not None and schedule_signature != self._schedule_signature:
                for job_id in (CYCLE_JOB_ID, MANUAL_JOB_ID):
                    if self.scheduler.get_job(job_id):
                        self.scheduler.remove_job(job_id)
                job = None
            if job is None:
                self.scheduler.add_job(
                    self.run_cycle,
                    trigger="interval",
                    minutes=configuration.send_interval_minutes,
                    args=[configuration.control_changed_at],
                    id=CYCLE_JOB_ID,
                    coalesce=True,
                    max_instances=1,
                    misfire_grace_time=5,
                    replace_existing=True,
                )
            if configuration.run_now_requested_at:
                self.scheduler.add_job(
                    self.run_cycle,
                    trigger="date",
                    run_date=timezone.now(),
                    args=[configuration.control_changed_at],
                    id=MANUAL_JOB_ID,
                    coalesce=True,
                    max_instances=1,
                    misfire_grace_time=5,
                    replace_existing=True,
                )
                Configuracao.objects.filter(
                    pk=configuration.pk,
                    run_now_requested_at=configuration.run_now_requested_at,
                ).update(run_now_requested_at=None, updated_at=timezone.now())

            scheduled_job = self.scheduler.get_job(CYCLE_JOB_ID)
            next_execution = scheduled_job.next_run_time if scheduled_job else None
        else:
            for job_id in (CYCLE_JOB_ID, MANUAL_JOB_ID):
                if self.scheduler.get_job(job_id):
                    self.scheduler.remove_job(job_id)
            next_execution = None

        self._schedule_signature = schedule_signature
        if configuration.next_execution != next_execution:
            Configuracao.objects.filter(pk=configuration.pk).update(
                next_execution=next_execution,
                updated_at=timezone.now(),
            )

    def run_cycle(self, expected_control_changed_at=None):
        if not self._cycle_lock.acquire(blocking=False):
            self._log(
                "Execução ignorada: já existe um ciclo de ofertas em andamento.",
                LogSistema.Severity.WARNING,
            )
            return
        try:
            configuration = Configuracao.current()
            if not self._is_running(configuration.pk, expected_control_changed_at):
                return

            started_at = timezone.now()
            Configuracao.objects.filter(pk=configuration.pk).update(
                last_execution=started_at,
                updated_at=started_at,
            )
            if not is_within_operating_hours(configuration, started_at):
                self._log(
                    "Ciclo ignorado: fora do horário de funcionamento configurado.",
                    LogSistema.Severity.INFO,
                    offer_search=True,
                )
                return
            if configuration.offer_provider == Configuracao.OfferProvider.DISABLED:
                self._log(
                    "Ciclo ignorado: o provedor de ofertas está desativado.",
                    LogSistema.Severity.WARNING,
                    offer_search=True,
                )
                return
            try:
                candidates = discover_flash_deals(self.discovery_provider)
            except OfferDiscoveryError as error:
                severity = (
                    LogSistema.Severity.WARNING
                    if isinstance(error, OfferDiscoveryRateLimited)
                    else LogSistema.Severity.ERROR
                )
                self._log(f"Busca de ofertas falhou: {error}", severity, offer_search=True)
                return

            checked_at = timezone.now()
            offers = []
            for candidate in candidates:
                if not self._is_running(configuration.pk, expected_control_changed_at):
                    self._log(
                        "Ciclo interrompido após pausa ou parada do robô.",
                        LogSistema.Severity.INFO,
                        offer_search=True,
                    )
                    return
                try:
                    offer = self._store_candidate(candidate, checked_at)
                except (ValidationError, IntegrityError, TypeError, ValueError) as error:
                    self._log(
                        f"Oferta do provedor ignorada por dados inválidos: {error}",
                        LogSistema.Severity.ERROR,
                        offer_search=True,
                    )
                    continue
                if offer is not None and self._is_currently_eligible(offer, checked_at):
                    offers.append(offer)

            offers.sort(
                key=lambda item: (item.discount_percent, item.last_checked_at),
                reverse=True,
            )
            for offer in offers:
                if not self._is_running(configuration.pk, expected_control_changed_at):
                    self._log(
                        "Ciclo interrompido antes de preparar ou enviar uma oferta.",
                        LogSistema.Severity.INFO,
                        offer_search=True,
                    )
                    return
                if self._product_has_active_or_sent_record(offer.product_id):
                    continue
                affiliate_link = (
                    offer.product.affiliate_links.filter(
                        validation_status=LinkAfiliado.ValidationStatus.VALID,
                    )
                    .order_by("-registered_at")
                    .first()
                )
                if affiliate_link is None:
                    continue
                if (
                    configuration.affiliate_provider
                    != Configuracao.AffiliateProvider.MANUAL_IMPORT
                ):
                    self._log(
                        f"Oferta {offer.pk} ignorada: o provedor de afiliados está desativado.",
                        LogSistema.Severity.WARNING,
                    )
                    continue
                if not configuration.whatsapp_destination:
                    self._log(
                        f"Oferta {offer.pk} ignorada: configure um destino antes de preparar mensagens.",
                        LogSistema.Severity.WARNING,
                    )
                    continue
                if (
                    configuration.whatsapp_provider
                    == Configuracao.WhatsAppProvider.DISABLED
                ):
                    self._log(
                        "Oferta elegível não processada: o provedor de WhatsApp está desativado.",
                        LogSistema.Severity.WARNING,
                    )
                    continue
                self._prepare_or_send(
                    configuration,
                    offer,
                    affiliate_link,
                    expected_control_changed_at,
                )
                return

            self._log(
                f"Ciclo concluído: {len(candidates)} ofertas consultadas; nenhuma elegível para divulgação.",
                LogSistema.Severity.INFO,
                offer_search=True,
            )
        finally:
            self._cycle_lock.release()

    def _store_candidate(self, candidate, checked_at):
        if not timezone.is_aware(candidate.starts_at):
            raise ValueError("A data de início da oferta precisa conter fuso horário.")
        if candidate.ends_at is not None and not timezone.is_aware(candidate.ends_at):
            raise ValueError("A data de término da oferta precisa conter fuso horário.")

        product = Produto.objects.filter(product_url=candidate.original_url).first()
        if product is None:
            product = Produto(
                name=candidate.title,
                external_id=candidate.external_id,
                image_url=candidate.image_url,
                product_url=candidate.original_url,
                category=candidate.category,
            )
        else:
            product.name = candidate.title
            product.external_id = candidate.external_id
            product.image_url = candidate.image_url
            product.category = candidate.category
        product.save()

        offer = Oferta.objects.filter(
            product=product,
            start_date=candidate.starts_at,
        ).first()
        if offer is None:
            offer = Oferta(product=product, start_date=candidate.starts_at)
        offer.original_price = candidate.original_price
        offer.promotional_price = candidate.promotional_price
        offer.end_date = candidate.ends_at
        offer.status = (
            Oferta.Status.AVAILABLE
            if candidate.is_available and (candidate.ends_at is None or candidate.ends_at > checked_at)
            else Oferta.Status.UNAVAILABLE
        )
        offer.last_checked_at = checked_at
        offer.save()
        return offer

    @staticmethod
    def _is_currently_eligible(offer, checked_at):
        return bool(
            offer.status == Oferta.Status.AVAILABLE
            and offer.original_price > 0
            and offer.promotional_price > 0
            and offer.promotional_price <= offer.original_price
            and offer.last_checked_at
            and offer.last_checked_at >= checked_at - timedelta(seconds=1)
            and offer.start_date <= checked_at
            and (offer.end_date is None or offer.end_date > checked_at)
        )

    @staticmethod
    def _product_has_active_or_sent_record(product_id):
        return Envio.objects.filter(
            product_id=product_id,
            status__in=[
                Envio.Status.PENDING,
                Envio.Status.PREPARED,
                Envio.Status.SENT,
                Envio.Status.FAILED,
            ],
        ).exists()

    def _prepare_or_send(
        self,
        configuration,
        offer,
        affiliate_link,
        expected_control_changed_at,
    ):
        try:
            prepared = prepare_offer_message(
                offer_id=offer.pk,
                destination=configuration.whatsapp_destination,
                require_validated_link=True,
            )
        except (WhatsAppPreparationError, IntegrityError) as error:
            self._log(
                f"Preparação da oferta {offer.pk} falhou: {error}",
                LogSistema.Severity.ERROR,
            )
            return

        message_record = prepared.record
        if message_record.status == MensagemWhatsApp.Status.FAILED:
            message_record.status = MensagemWhatsApp.Status.PREPARED
            message_record.error_detail = ""
            message_record.save(update_fields=["status", "error_detail"])

        can_deliver = bool(
            configuration.whatsapp_provider == Configuracao.WhatsAppProvider.OFFICIAL_API
            and is_official_delivery_ready(
                configuration.whatsapp_destination,
                provider=self.delivery_provider,
            )
        )
        try:
            envio = Envio.objects.create(
                product=offer.product,
                offer=offer,
                message=message_record.message,
                destination=configuration.whatsapp_destination,
                product_name_snapshot=offer.product.name,
                price_snapshot=offer.promotional_price,
                discount_snapshot=offer.discount_percent,
                affiliate_url=affiliate_link.affiliate_url,
                status=Envio.Status.PENDING if can_deliver else Envio.Status.PREPARED,
            )
        except (ValidationError, IntegrityError) as error:
            message_record.status = MensagemWhatsApp.Status.FAILED
            message_record.error_detail = str(error)[:500]
            message_record.save(update_fields=["status", "error_detail"])
            self._log(
                f"Registro da oferta {offer.pk} não pôde ser criado: {error}",
                LogSistema.Severity.ERROR,
            )
            return
        if not can_deliver:
            self._log(
                f"Oferta {offer.pk} preparada, mas não enviada: não há provedor autorizado, conectado e compatível com o destino.",
                LogSistema.Severity.WARNING,
            )
            return

        if not self._is_running(configuration.pk, expected_control_changed_at):
            envio.status = Envio.Status.CANCELLED
            envio.save(update_fields=["status"])
            self._log(
                f"Envio da oferta {offer.pk} cancelado porque o robô foi interrompido.",
                LogSistema.Severity.WARNING,
            )
            return

        try:
            external_id = self.delivery_provider.send(
                configuration.whatsapp_destination,
                message_record.message,
            )
            if not isinstance(external_id, str) or not external_id.strip():
                raise WhatsAppDeliveryUnavailable(
                    "O provedor não confirmou o identificador do envio."
                )
        except WhatsAppDeliveryUnavailable as error:
            envio.status = Envio.Status.FAILED
            envio.save(update_fields=["status"])
            message_record.status = MensagemWhatsApp.Status.FAILED
            message_record.error_detail = str(error)[:500]
            message_record.save(update_fields=["status", "error_detail"])
            self._log(
                f"Envio automático da oferta {offer.pk} falhou: {error}",
                LogSistema.Severity.ERROR,
                event_type=LogSistema.EventType.SEND,
            )
            return

        envio.status = Envio.Status.SENT
        envio.external_id = external_id.strip()[:200]
        envio.save(update_fields=["status", "external_id"])
        message_record.status = MensagemWhatsApp.Status.SENT
        message_record.save(update_fields=["status"])
        self._log(
            f"Provedor confirmou o envio da oferta {offer.pk} (ID {envio.external_id}).",
            LogSistema.Severity.INFO,
            event_type=LogSistema.EventType.SEND,
        )

    @staticmethod
    def _is_running(configuration_id, expected_control_changed_at=None):
        return Configuracao.objects.filter(
            pk=configuration_id,
            status=Configuracao.Status.RUNNING,
            control_changed_at=expected_control_changed_at,
        ).exists()

    @staticmethod
    def _log(
        description,
        severity,
        offer_search=False,
        event_type=LogSistema.EventType.SYSTEM,
    ):
        if offer_search:
            event_type = LogSistema.EventType.OFFER_SEARCH
        LogSistema.objects.create(
            event_type=event_type,
            description=description[:1000],
            severity=severity,
        )


def record_scheduler_error(event):
    if event.exception is None:
        return
    LogSistema.objects.create(
        event_type=LogSistema.EventType.SYSTEM,
        description=f"Falha no job APScheduler {event.job_id}: {event.exception}"[:1000],
        severity=LogSistema.Severity.ERROR,
    )
