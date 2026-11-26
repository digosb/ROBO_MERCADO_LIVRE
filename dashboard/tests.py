import json
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from apscheduler.schedulers.background import BackgroundScheduler

from .automation import (
    CYCLE_JOB_ID,
    MANUAL_JOB_ID,
    AutomationRuntime,
    is_worker_online,
    is_within_operating_hours,
)
from .mercadolivre import (
    OfferCandidate,
    OfferDiscoveryRateLimited,
    OfferDiscoveryUnavailable,
    OfferProviderError,
    discover_flash_deals,
)
from .afiliados import import_manual_affiliate_link
from .models import (
    Configuracao,
    Envio,
    LinkAfiliado,
    LogSistema,
    MensagemWhatsApp,
    Oferta,
    Produto,
)
from .reporting import build_report_metrics
from .whatsapp import (
    WhatsAppDeliveryUnavailable,
    WhatsAppPreparationError,
    get_delivery_provider,
    prepare_offer_message,
)


def create_product(index=1, **overrides):
    values = {
        "name": f"Produto {index}",
        "external_id": f"MLB-{index}",
        "product_url": f"https://example.com/produto-{index}",
    }
    values.update(overrides)
    return Produto.objects.create(**values)


def create_offer(product, **overrides):
    values = {
        "product": product,
        "original_price": Decimal("100.00"),
        "promotional_price": Decimal("75.00"),
    }
    values.update(overrides)
    return Oferta.objects.create(**values)


class DashboardPageTests(TestCase):
    def test_dashboard_starts_with_empty_metrics_and_explicit_integration_state(self):
        response = self.client.get(reverse("dashboard:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "0")
        self.assertContains(response, "Integrações externas indisponíveis")
        self.assertContains(response, "Worker desconectado")
        self.assertEqual(response.context["metrics"]["offers_found"], 0)
        self.assertEqual(response.context["metrics"]["messages_sent"], 0)

    def test_navigation_pages_render(self):
        for name in (
            "dashboard:offers",
            "dashboard:affiliates",
            "dashboard:whatsapp",
            "dashboard:history",
            "dashboard:reports",
            "dashboard:settings",
        ):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_dashboard_shows_internal_alerts_only_when_enabled(self):
        LogSistema.objects.create(
            description="Alerta interno de teste",
            severity=LogSistema.Severity.WARNING,
        )
        self.assertNotContains(
            self.client.get(reverse("dashboard:dashboard")),
            "Alerta interno de teste",
        )

        configuration = Configuracao.current()
        configuration.notifications_enabled = True
        configuration.save(update_fields=["notifications_enabled", "updated_at"])

        self.assertContains(
            self.client.get(reverse("dashboard:dashboard")),
            "Alerta interno de teste",
        )


class RobotEndpointTests(TestCase):
    def test_robot_actions_change_persisted_status(self):
        configuration = Configuracao.current()
        configuration.worker_heartbeat = timezone.now()
        configuration.save(update_fields=["worker_heartbeat", "updated_at"])

        for action, expected in (
            ("start", Configuracao.Status.RUNNING),
            ("pause", Configuracao.Status.PAUSED),
            ("resume", Configuracao.Status.RUNNING),
            ("stop", Configuracao.Status.STOPPED),
        ):
            response = self.client.post(reverse("robot_action", args=[action]))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["success"])
            self.assertEqual(Configuracao.current().status, expected)

    def test_pause_rejects_stopped_robot(self):
        response = self.client.post(reverse("robot_action", args=["pause"]))

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["success"])

    def test_start_requires_a_live_worker(self):
        response = self.client.post(reverse("robot_action", args=["start"]))

        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["success"])
        self.assertEqual(Configuracao.current().status, Configuracao.Status.STOPPED)

    def test_search_queues_a_real_worker_request(self):
        configuration = Configuracao.current()
        configuration.status = Configuracao.Status.RUNNING
        configuration.worker_heartbeat = timezone.now()
        configuration.save(update_fields=["status", "worker_heartbeat", "updated_at"])

        response = self.client.post(reverse("robot_action", args=["search"]))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertIsNotNone(Configuracao.current().run_now_requested_at)
        self.assertIsNone(Configuracao.current().last_execution)
        self.assertEqual(Oferta.objects.count(), 0)

    def test_search_requires_running_robot_and_worker(self):
        response = self.client.post(reverse("robot_action", args=["search"]))

        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["success"])

    def test_unknown_action_is_not_found(self):
        response = self.client.post(reverse("robot_action", args=["reboot"]))

        self.assertEqual(response.status_code, 404)


class AffiliateLinkImportTests(TestCase):
    def test_import_supports_long_product_and_affiliate_urls(self):
        original_url = f"https://example.com/produto?referencia={'a' * 220}"
        affiliate_url = f"https://example.com/affiliate?referencia={'b' * 220}"
        product = create_product(product_url=original_url)

        result = import_manual_affiliate_link(original_url, affiliate_url)

        self.assertGreater(len(result.link.original_url), 200)
        self.assertGreater(len(result.link.affiliate_url), 200)

    def test_generation_date_is_not_assumed_when_model_is_created_directly(self):
        product = create_product()

        link = LinkAfiliado.objects.create(
            product=product,
            original_url=product.product_url,
            affiliate_url="https://example.com/afiliado-sem-data",
        )

        self.assertIsNone(link.generated_at)

    def test_manual_import_associates_product_and_keeps_affiliate_validation_pending(self):
        product = create_product()
        response = self.client.post(
            reverse("dashboard:affiliates"),
            {
                "original_url": product.product_url,
                "affiliate_url": "https://example.com/afiliado-validacao-manual",
                "generated_date": "2026-11-25",
            },
            follow=True,
        )

        self.assertContains(response, "validação de afiliação permanece pendente")
        link = LinkAfiliado.objects.get()
        self.assertEqual(link.product, product)
        self.assertEqual(link.original_url, product.product_url)
        self.assertEqual(
            link.validation_status,
            LinkAfiliado.ValidationStatus.PENDING,
        )
        self.assertEqual(
            timezone.localtime(link.generated_at).date().isoformat(),
            "2026-11-25",
        )
        self.assertIsNotNone(link.registered_at)
        self.assertEqual(
            LogSistema.objects.get().event_type,
            LogSistema.EventType.AFFILIATE_IMPORT,
        )

    def test_duplicate_manual_import_is_idempotent(self):
        product = create_product()
        values = {
            "original_url": product.product_url,
            "affiliate_url": "https://example.com/afiliado-duplicado",
        }

        first = import_manual_affiliate_link(**values)
        second = import_manual_affiliate_link(**values)

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.link.pk, second.link.pk)
        self.assertIsNone(first.link.generated_at)
        self.assertEqual(LinkAfiliado.objects.count(), 1)
        self.assertEqual(LogSistema.objects.count(), 2)

    def test_manual_import_rejects_unknown_product_and_non_http_urls(self):
        unknown_product = self.client.post(
            reverse("dashboard:affiliates"),
            {
                "original_url": "https://example.com/not-catalogued",
                "affiliate_url": "https://example.com/affiliate",
            },
            follow=True,
        )
        invalid_url = self.client.post(
            reverse("dashboard:affiliates"),
            {
                "original_url": "https://example.com/produto",
                "affiliate_url": "javascript:alert(1)",
            },
            follow=True,
        )

        self.assertContains(unknown_product, "Não existe produto cadastrado")
        self.assertContains(invalid_url, "URL HTTP ou HTTPS válida")
        self.assertEqual(LinkAfiliado.objects.count(), 0)
        self.assertEqual(
            LogSistema.objects.filter(
                event_type=LogSistema.EventType.AFFILIATE_IMPORT,
                severity=LogSistema.Severity.WARNING,
            ).count(),
            2,
        )

    def test_manual_import_rejects_embedded_credentials_and_invalid_generation_date(self):
        product = create_product()
        credential_in_url = self.client.post(
            reverse("dashboard:affiliates"),
            {
                "original_url": product.product_url,
                "affiliate_url": "https://user:password@example.com/affiliate",
            },
            follow=True,
        )
        invalid_date = self.client.post(
            reverse("dashboard:affiliates"),
            {
                "original_url": product.product_url,
                "affiliate_url": "https://example.com/affiliate",
                "generated_date": "2026-02-30",
            },
            follow=True,
        )

        self.assertContains(credential_in_url, "não pode conter credenciais")
        self.assertContains(invalid_date, "data de geração informada é inválida")
        self.assertEqual(LinkAfiliado.objects.count(), 0)
        self.assertEqual(
            LogSistema.objects.filter(
                event_type=LogSistema.EventType.AFFILIATE_IMPORT,
                severity=LogSistema.Severity.WARNING,
            ).count(),
            2,
        )

    def test_affiliates_page_shows_original_affiliate_and_generation_date(self):
        product = create_product()
        result = import_manual_affiliate_link(
            product.product_url,
            "https://example.com/afiliado-pagina",
            "2026-11-24",
        )

        response = self.client.get(reverse("dashboard:affiliates"))

        self.assertContains(response, product.name)
        self.assertContains(response, product.product_url)
        self.assertContains(response, result.link.affiliate_url)
        self.assertContains(response, "24/11/2026")
        self.assertContains(response, "Pendente")


class HistoryAndReportTests(TestCase):
    def test_history_filters_real_sending_records_and_displays_snapshots(self):
        today = timezone.localtime(timezone.now()).date()
        sent_product = create_product(name="Fone divulgado")
        sent_offer = create_offer(
            sent_product,
            promotion_type=Oferta.PromotionType.DISCOUNT,
            status=Oferta.Status.AVAILABLE,
        )
        failed_product = create_product(2, name="Caixa de som")
        failed_offer = create_offer(
            failed_product,
            promotion_type=Oferta.PromotionType.COUPON,
        )
        Envio.objects.create(
            product=sent_product,
            offer=sent_offer,
            message="Mensagem enviada",
            destination="Grupo público",
            product_name_snapshot="Fone divulgado",
            price_snapshot=Decimal("75.00"),
            discount_snapshot=Decimal("25.00"),
            affiliate_url="https://example.com/affiliate-fone",
            status=Envio.Status.SENT,
        )
        Envio.objects.create(
            product=failed_product,
            offer=failed_offer,
            message="Mensagem falha",
            destination="Grupo público",
            status=Envio.Status.FAILED,
            created_at=timezone.make_aware(datetime.combine(today - timedelta(days=1), time(12))),
        )
        response = self.client.get(
            reverse("dashboard:history"),
            {
                "date_from": today.isoformat(),
                "date_to": today.isoformat(),
                "product": "Fone",
                "status": Envio.Status.SENT,
                "offer_type": Oferta.PromotionType.DISCOUNT,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Fone divulgado")
        self.assertContains(response, "R$ 75,00")
        self.assertContains(response, "25.00%")
        self.assertContains(response, "https://example.com/affiliate-fone")
        self.assertContains(response, "Grupo público")
        self.assertContains(response, "Enviada")
        self.assertNotContains(response, "Caixa de som")
        self.assertEqual(response.context["sendings_page"].paginator.count, 1)

    def test_history_rejects_reversed_date_range_without_returning_unfiltered_rows(self):
        product = create_product()
        offer = create_offer(product)
        Envio.objects.create(
            product=product,
            offer=offer,
            message="Mensagem",
            destination="Grupo",
            status=Envio.Status.SENT,
        )
        today = timezone.localtime(timezone.now()).date()

        response = self.client.get(
            reverse("dashboard:history"),
            {
                "date_from": (today + timedelta(days=1)).isoformat(),
                "date_to": today.isoformat(),
            },
        )

        self.assertContains(response, "A data final deve ser igual ou posterior à inicial.")
        self.assertEqual(response.context["sendings_page"].paginator.count, 0)

    def test_report_indicators_use_persisted_records_and_count_failures_once(self):
        approved_product = create_product(name="Oferta aprovada")
        approved_offer = create_offer(
            approved_product,
            status=Oferta.Status.AVAILABLE,
        )
        pending_product = create_product(2, name="Oferta não verificada")
        pending_offer = create_offer(
            pending_product,
            status=Oferta.Status.UNKNOWN,
        )
        LinkAfiliado.objects.create(
            product=approved_product,
            original_url=approved_product.product_url,
            affiliate_url="https://example.com/valid-report-link",
            validation_status=LinkAfiliado.ValidationStatus.VALID,
        )
        LinkAfiliado.objects.create(
            product=pending_product,
            original_url=pending_product.product_url,
            affiliate_url="https://example.com/pending-report-link",
            validation_status=LinkAfiliado.ValidationStatus.PENDING,
        )
        Envio.objects.create(
            product=approved_product,
            offer=approved_offer,
            message="Enviada",
            destination="Grupo",
            status=Envio.Status.SENT,
        )
        Envio.objects.create(
            product=pending_product,
            offer=pending_offer,
            message="Falhou",
            destination="Grupo",
            status=Envio.Status.FAILED,
        )
        LogSistema.objects.create(
            event_type=LogSistema.EventType.OFFER_SEARCH,
            description="Falha real do provedor de busca",
            severity=LogSistema.Severity.ERROR,
        )
        LogSistema.objects.create(
            event_type=LogSistema.EventType.SEND,
            description="Falha de envio já contada no registro",
            severity=LogSistema.Severity.ERROR,
        )

        metrics = build_report_metrics()
        response = self.client.get(reverse("dashboard:reports"))

        self.assertEqual(metrics["offers_found"], 2)
        self.assertEqual(metrics["offers_approved"], 1)
        self.assertEqual(metrics["valid_affiliate_links"], 1)
        self.assertEqual(metrics["messages_sent"], 1)
        self.assertEqual(metrics["failures"], 2)
        self.assertEqual(metrics["success_percentage"], Decimal("50.00"))
        self.assertContains(response, "Percentual de sucesso")
        self.assertContains(response, "50.00%")


class ConfigurationTests(TestCase):
    def settings_data(self, **overrides):
        configuration = Configuracao.current()
        data = {
            "send_interval_minutes": str(configuration.send_interval_minutes),
            "max_sends_per_day": str(configuration.max_sends_per_day),
            "operating_hours_enabled": (
                "on" if configuration.operating_hours_enabled else ""
            ),
            "operating_start_time": configuration.operating_start_time.strftime("%H:%M"),
            "operating_end_time": configuration.operating_end_time.strftime("%H:%M"),
            "whatsapp_destination": configuration.whatsapp_destination,
            "notifications_enabled": (
                "on" if configuration.notifications_enabled else ""
            ),
            "offer_provider": configuration.offer_provider,
            "affiliate_provider": configuration.affiliate_provider,
            "whatsapp_provider": configuration.whatsapp_provider,
        }
        data.update(overrides)
        return data

    def test_configuration_defaults_to_ten_minutes_and_fifty_sends(self):
        response = self.client.get(reverse("dashboard:settings"))

        self.assertEqual(response.context["settings"].send_interval_minutes, 10)
        self.assertEqual(response.context["settings"].max_sends_per_day, 50)

    def test_settings_validate_and_persist_interval_and_daily_limit(self):
        url = reverse("dashboard:settings")
        for invalid in ("0", "1441", "abc"):
            with self.subTest(interval=invalid):
                self.client.post(
                    url,
                    self.settings_data(send_interval_minutes=invalid, max_sends_per_day="12"),
                )
                self.assertEqual(Configuracao.current().send_interval_minutes, 10)
                self.assertEqual(Configuracao.current().max_sends_per_day, 50)

        self.client.post(
            url,
            self.settings_data(send_interval_minutes="15", max_sends_per_day="12"),
        )
        self.assertEqual(Configuracao.current().send_interval_minutes, 15)
        self.assertEqual(Configuracao.current().max_sends_per_day, 12)

        self.client.post(
            url,
            self.settings_data(send_interval_minutes="15", max_sends_per_day="10001"),
        )
        self.assertEqual(Configuracao.current().max_sends_per_day, 12)

    def test_settings_validate_hours_destination_and_provider_choices(self):
        url = reverse("dashboard:settings")
        initial = self.settings_data()

        invalid_hours = self.client.post(
            url,
            {
                **initial,
                "operating_hours_enabled": "on",
                "operating_start_time": "09:00",
                "operating_end_time": "09:00",
            },
        )
        self.assertEqual(invalid_hours.status_code, 200)
        self.assertEqual(Configuracao.current().operating_hours_enabled, False)

        invalid_destination = self.client.post(
            url,
            {**initial, "whatsapp_destination": "https://example.com/group"},
        )
        self.assertEqual(invalid_destination.status_code, 200)
        self.assertEqual(Configuracao.current().whatsapp_destination, "")

        invalid_provider = self.client.post(
            url,
            {**initial, "offer_provider": "not-a-provider"},
        )
        self.assertEqual(invalid_provider.status_code, 200)
        self.assertEqual(
            Configuracao.current().offer_provider,
            Configuracao.OfferProvider.MARKETPLACE,
        )

    def test_settings_persist_operating_hours_notifications_and_available_providers(self):
        response = self.client.post(
            reverse("dashboard:settings"),
            self.settings_data(
                operating_hours_enabled="on",
                operating_start_time="22:00",
                operating_end_time="06:00",
                whatsapp_destination="Grupo de ofertas",
                notifications_enabled="on",
                offer_provider=Configuracao.OfferProvider.DISABLED,
                affiliate_provider=Configuracao.AffiliateProvider.DISABLED,
                whatsapp_provider=Configuracao.WhatsAppProvider.DISABLED,
            ),
        )

        self.assertEqual(response.status_code, 302)
        configuration = Configuracao.current()
        self.assertTrue(configuration.operating_hours_enabled)
        self.assertEqual(configuration.operating_start_time, time(22, 0))
        self.assertEqual(configuration.operating_end_time, time(6, 0))
        self.assertEqual(configuration.whatsapp_destination, "Grupo de ofertas")
        self.assertTrue(configuration.notifications_enabled)
        self.assertEqual(configuration.offer_provider, Configuracao.OfferProvider.DISABLED)
        self.assertEqual(
            configuration.affiliate_provider,
            Configuracao.AffiliateProvider.DISABLED,
        )
        self.assertEqual(
            configuration.whatsapp_provider,
            Configuracao.WhatsAppProvider.DISABLED,
        )

    def test_official_whatsapp_provider_cannot_be_enabled_without_eligibility(self):
        configuration = Configuracao.current()
        configuration.whatsapp_provider = Configuracao.WhatsAppProvider.OFFICIAL_API

        with self.assertRaises(ValidationError):
            configuration.save()

    def test_operating_window_supports_overnight_ranges(self):
        configuration = Configuracao.current()
        configuration.operating_hours_enabled = True
        configuration.operating_start_time = time(22, 0)
        configuration.operating_end_time = time(6, 0)
        evening = timezone.make_aware(datetime(2026, 11, 26, 23, 0))
        early_morning = timezone.make_aware(datetime(2026, 11, 27, 5, 0))
        midday = timezone.make_aware(datetime(2026, 11, 27, 12, 0))

        self.assertTrue(is_within_operating_hours(configuration, evening))
        self.assertTrue(is_within_operating_hours(configuration, early_morning))
        self.assertFalse(is_within_operating_hours(configuration, midday))

    def test_whatsapp_preview_requires_destination_and_does_not_send(self):
        url = reverse("whatsapp_prepare")
        product = create_product()
        offer = create_offer(product)
        LinkAfiliado.objects.create(
            product=product,
            original_url=product.product_url,
            affiliate_url="https://example.com/afiliado",
        )
        body = json.dumps({"offer_id": offer.pk, "is_test": True})

        missing_destination = self.client.post(
            url, body, content_type="application/json"
        )
        self.assertEqual(missing_destination.status_code, 400)
        self.assertEqual(
            MensagemWhatsApp.objects.get().status,
            MensagemWhatsApp.Status.FAILED,
        )

        configuration = Configuracao.current()
        configuration.whatsapp_destination = "Grupo público de ofertas"
        configuration.save()
        response = self.client.post(url, body, content_type="application/json")

        self.assertEqual(response.status_code, 200)
        self.assertIn("nenhuma mensagem foi enviada", response.json()["message"])
        self.assertEqual(response.json()["destination"], configuration.whatsapp_destination)
        self.assertEqual(Envio.objects.count(), 0)
        self.assertEqual(MensagemWhatsApp.objects.count(), 2)
        self.assertEqual(LogSistema.objects.count(), 2)

    def test_whatsapp_preview_rejects_malformed_and_non_object_json(self):
        configuration = Configuracao.current()
        configuration.whatsapp_destination = "Grupo de teste"
        configuration.save()
        url = reverse("whatsapp_prepare")

        malformed = self.client.post(url, "{", content_type="application/json")
        non_object = self.client.post(url, "[]", content_type="application/json")

        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(non_object.status_code, 400)

    def test_whatsapp_message_uses_offer_and_affiliate_link_and_deduplicates(self):
        product = create_product(name="Cafeteira")
        offer = create_offer(
            product,
            original_price=Decimal("349.90"),
            promotional_price=Decimal("279.92"),
        )
        LinkAfiliado.objects.create(
            product=product,
            original_url=product.product_url,
            affiliate_url="https://example.com/afiliado-cafeteira",
        )
        values = {
            "offer_id": offer.pk,
            "destination": "Grupo público",
            "is_test": True,
        }

        first = prepare_offer_message(**values)
        second = prepare_offer_message(**values)

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.record.pk, second.record.pk)
        self.assertIn("🔥 OFERTA RELÂMPAGO!", first.record.message)
        self.assertIn("📦 Cafeteira", first.record.message)
        self.assertIn("❌ De: R$ 349,90", first.record.message)
        self.assertIn("✅ Por: R$ 279,92", first.record.message)
        self.assertIn("🔥 Desconto: 20,00%", first.record.message)
        self.assertIn("https://example.com/afiliado-cafeteira", first.record.message)
        self.assertEqual(MensagemWhatsApp.objects.count(), 1)
        self.assertEqual(Envio.objects.count(), 0)

    def test_whatsapp_preparation_records_failure_without_affiliate_link(self):
        offer = create_offer(create_product())

        with self.assertRaises(WhatsAppPreparationError):
            prepare_offer_message(offer.pk, "Grupo público")

        failure = MensagemWhatsApp.objects.get()
        self.assertEqual(failure.status, MensagemWhatsApp.Status.FAILED)
        self.assertIn("link de afiliado", failure.error_detail)
        self.assertEqual(Envio.objects.count(), 0)

    def test_whatsapp_destination_rejects_invitation_urls(self):
        response = self.client.post(
            reverse("dashboard:whatsapp"),
            {"destination": "https://chat.whatsapp.com/example"},
            follow=True,
        )

        self.assertContains(response, "rótulo para o grupo. Links de convite")
        self.assertEqual(Configuracao.current().whatsapp_destination, "")

    def test_official_delivery_provider_fails_closed_for_personal_existing_group(self):
        provider = get_delivery_provider()

        with self.assertRaises(WhatsAppDeliveryUnavailable):
            provider.send("Grupo público", "Mensagem")

        self.assertEqual(Envio.objects.count(), 0)

    def test_worker_heartbeat_expires(self):
        configuration = Configuracao.current()
        configuration.worker_heartbeat = timezone.now() - timedelta(seconds=30)
        configuration.save(update_fields=["worker_heartbeat", "updated_at"])

        self.assertFalse(is_worker_online(configuration))

    def test_whatsapp_page_displays_connection_limitation_and_history(self):
        product = create_product()
        offer = create_offer(product)
        LinkAfiliado.objects.create(
            product=product,
            original_url=product.product_url,
            affiliate_url="https://example.com/afiliado",
        )
        prepare_offer_message(offer.pk, "Grupo público")

        response = self.client.get(reverse("dashboard:whatsapp"))

        self.assertContains(response, "grupos existentes do WhatsApp pessoal")
        self.assertContains(response, "Preparada")
        self.assertContains(response, "Grupo público")
        self.assertContains(response, "Envios reais")

    def test_dashboard_metrics_use_persisted_business_records(self):
        product = create_product()
        offer = create_offer(product, status=Oferta.Status.AVAILABLE)
        LinkAfiliado.objects.create(
            product=product,
            original_url=product.product_url,
            affiliate_url="https://example.com/afiliado-1",
        )
        Envio.objects.create(
            product=product,
            offer=offer,
            message="Mensagem registrada",
            destination="+5511999999999",
            status=Envio.Status.SENT,
        )
        LogSistema.objects.create(
            event_type=LogSistema.EventType.SEND,
            description="Falha registrada",
            severity=LogSistema.Severity.ERROR,
        )

        response = self.client.get(reverse("api_status"))
        metrics = response.json()["metrics"]

        self.assertEqual(metrics["offers_found"], 1)
        self.assertEqual(metrics["valid_offers"], 1)
        self.assertEqual(metrics["affiliate_links"], 1)
        self.assertEqual(metrics["messages_sent"], 1)
        self.assertEqual(metrics["errors"], 1)
        self.assertEqual(response.json()["max_sends_per_day"], 50)


class BusinessModelTests(TestCase):
    def test_product_duplicate_external_id_and_original_url_are_rejected(self):
        product = create_product(external_id=" MLB-duplicate ")

        self.assertEqual(product.external_id, "mlb-duplicate")
        with self.assertRaises(ValidationError):
            create_product(
                2,
                external_id="MLB-DUPLICATE",
                product_url="https://example.com/produto-2",
            )
        with self.assertRaises(ValidationError):
            create_product(
                3,
                external_id="MLB-3",
                product_url=product.product_url,
            )

    def test_offer_calculates_discount_and_validates_prices_and_dates(self):
        product = create_product()
        offer = create_offer(product)

        self.assertEqual(offer.discount_percent, Decimal("25.00"))

        with self.assertRaises(ValidationError):
            create_offer(product, promotional_price=Decimal("101.00"))
        with self.assertRaises(ValidationError):
            create_offer(
                product,
                end_date=timezone.now() - timedelta(days=1),
                start_date=timezone.now(),
            )

    def test_affiliate_link_must_belong_to_product_original_url(self):
        product = create_product()

        with self.assertRaises(ValidationError):
            LinkAfiliado.objects.create(
                product=product,
                original_url="https://example.com/outro-produto",
                affiliate_url="https://example.com/afiliado",
            )

        LinkAfiliado.objects.create(
            product=product,
            original_url=product.product_url,
            affiliate_url="https://example.com/afiliado",
        )
        with self.assertRaises(ValidationError):
            LinkAfiliado.objects.create(
                product=product,
                original_url=product.product_url,
                affiliate_url="https://example.com/afiliado",
            )

    def test_envio_rejects_duplicate_active_offer_and_allows_retry_after_failure(self):
        product = create_product()
        offer = create_offer(product)
        first = Envio.objects.create(
            product=product,
            offer=offer,
            message="Mensagem",
            destination="+5511999999999",
        )

        with self.assertRaises(ValidationError):
            Envio.objects.create(
                product=product,
                offer=offer,
                message="Duplicada",
                destination="+5511999999999",
            )

        first.status = Envio.Status.FAILED
        first.save()
        retry = Envio.objects.create(
            product=product,
            offer=offer,
            message="Nova tentativa",
            destination="+5511999999999",
        )
        retry.status = Envio.Status.SENT
        retry.save()
        with self.assertRaises(ValidationError):
            Envio.objects.create(
                product=product,
                offer=offer,
                message="Reenvio",
                destination="+5511999999999",
            )

    def test_database_constraint_blocks_duplicate_even_when_validation_is_bypassed(self):
        product = create_product()
        offer = create_offer(product)
        Envio.objects.create(
            product=product,
            offer=offer,
            message="Mensagem",
            destination="+5511999999999",
        )
        duplicate = Envio(
            product=product,
            offer=offer,
            message="Duplicada",
            destination="+5511999999999",
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Envio.objects.bulk_create([duplicate])

    def test_daily_send_limit_counts_pending_and_sent_records(self):
        configuration = Configuracao.current()
        configuration.max_sends_per_day = 1
        configuration.save()
        first_product = create_product()
        first_offer = create_offer(first_product)
        Envio.objects.create(
            product=first_product,
            offer=first_offer,
            message="Mensagem enviada",
            destination="+5511999999999",
            status=Envio.Status.SENT,
        )

        second_product = create_product(
            2,
            product_url="https://example.com/produto-2",
        )
        second_offer = create_offer(second_product)
        with self.assertRaises(ValidationError):
            Envio.objects.create(
                product=second_product,
                offer=second_offer,
                message="Além do limite",
                destination="+5511999999999",
            )

    def test_prepared_send_reserves_offer_and_daily_capacity(self):
        configuration = Configuracao.current()
        configuration.max_sends_per_day = 1
        configuration.save()
        product = create_product()
        offer = create_offer(product)
        Envio.objects.create(
            product=product,
            offer=offer,
            message="Preparada, ainda não enviada",
            destination="Grupo",
            status=Envio.Status.PREPARED,
        )

        with self.assertRaises(ValidationError):
            Envio.objects.create(
                product=product,
                offer=offer,
                message="Duplicada",
                destination="Grupo",
                status=Envio.Status.PENDING,
            )

        second_product = create_product(2, product_url="https://example.com/produto-2")
        with self.assertRaises(ValidationError):
            Envio.objects.create(
                product=second_product,
                offer=create_offer(second_product),
                message="Além do limite",
                destination="Grupo",
                status=Envio.Status.PREPARED,
            )

    def test_sending_product_must_match_offer(self):
        product = create_product()
        other_product = create_product(2)
        offer = create_offer(product)

        with self.assertRaises(ValidationError):
            Envio.objects.create(
                product=other_product,
                offer=offer,
                message="Mensagem",
                destination="+5511999999999",
            )

    def test_execution_settings_must_be_a_json_object(self):
        configuration = Configuracao.current()
        configuration.execution_settings = ["not", "an", "object"]

        with self.assertRaises(ValidationError):
            configuration.save()


class MercadoLivreDiscoveryTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_marketplace_flash_deal_provider_is_explicitly_unavailable(self):
        with self.assertRaises(OfferDiscoveryUnavailable):
            discover_flash_deals()

    def test_discovery_caches_results_from_an_available_provider(self):
        candidate = OfferCandidate(
            external_id="TEST-ITEM-1",
            title="Produto de teste",
            image_url="https://example.com/test-image.jpg",
            original_url="https://example.com/test-item",
            category="Teste",
            original_price=Decimal("100.00"),
            promotional_price=Decimal("80.00"),
            starts_at=timezone.now(),
            ends_at=None,
            is_available=True,
        )

        class TestProvider:
            cache_namespace = "unit-test-cache"
            is_available = True
            calls = 0

            def discover(self):
                self.calls += 1
                return [candidate]

        provider = TestProvider()

        first_result = discover_flash_deals(provider)
        second_result = discover_flash_deals(provider)

        self.assertEqual(first_result, [candidate])
        self.assertEqual(second_result, [candidate])
        self.assertEqual(provider.calls, 1)

    def test_failed_provider_is_throttled_and_failure_is_not_cached(self):
        class TestProvider:
            cache_namespace = "unit-test-throttle"
            is_available = True
            calls = 0

            def discover(self):
                self.calls += 1
                raise OfferProviderError("Falha controlada pelo teste.")

        provider = TestProvider()

        with self.assertRaises(OfferProviderError):
            discover_flash_deals(provider)
        with self.assertRaises(OfferDiscoveryRateLimited):
            discover_flash_deals(provider)

        self.assertEqual(provider.calls, 1)


class AutomationCycleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.now = timezone.now()
        self.candidate = OfferCandidate(
            external_id="TEST-AUTOMATION-1",
            title="Produto automatizado",
            image_url="https://example.com/automation-image.jpg",
            original_url="https://example.com/automation-product",
            category="Teste",
            original_price=Decimal("100.00"),
            promotional_price=Decimal("75.00"),
            starts_at=self.now - timedelta(minutes=1),
            ends_at=self.now + timedelta(hours=1),
            is_available=True,
        )
        self.configuration = Configuracao.current()
        self.configuration.status = Configuracao.Status.RUNNING
        self.configuration.whatsapp_destination = "Grupo de ofertas"
        self.configuration.save(update_fields=["status", "whatsapp_destination", "updated_at"])

    def make_provider(self, candidates=None, on_discover=None):
        class TestProvider:
            cache_namespace = "automation-cycle-tests"
            is_available = True

            def discover(inner_self):
                if on_discover:
                    on_discover()
                return candidates if candidates is not None else [self.candidate]

        return TestProvider()

    def add_verified_link(self):
        product = create_product(
            name=self.candidate.title,
            external_id=self.candidate.external_id,
            product_url=self.candidate.original_url,
        )
        LinkAfiliado.objects.create(
            product=product,
            original_url=product.product_url,
            affiliate_url="https://example.com/validated-affiliate",
            validation_status=LinkAfiliado.ValidationStatus.VALID,
        )
        return product

    def test_cycle_prepares_once_without_claiming_delivery(self):
        self.add_verified_link()
        runtime = AutomationRuntime(
            scheduler=None,
            discovery_provider=self.make_provider(),
        )

        runtime.run_cycle()
        runtime.run_cycle()

        envio = Envio.objects.get()
        self.assertEqual(envio.status, Envio.Status.PREPARED)
        self.assertEqual(envio.product_name_snapshot, self.candidate.title)
        self.assertEqual(envio.price_snapshot, Decimal("75.00"))
        self.assertEqual(envio.discount_snapshot, Decimal("25.00"))
        self.assertEqual(
            envio.affiliate_url,
            "https://example.com/validated-affiliate",
        )
        self.assertEqual(MensagemWhatsApp.objects.get().status, MensagemWhatsApp.Status.PREPARED)
        self.assertEqual(Envio.objects.filter(status=Envio.Status.SENT).count(), 0)
        self.assertEqual(Envio.objects.filter(product=envio.product).count(), 1)

    def test_cycle_does_not_prepare_or_send_when_delivery_is_disabled(self):
        self.add_verified_link()
        Configuracao.objects.filter(pk=self.configuration.pk).update(
            whatsapp_provider=Configuracao.WhatsAppProvider.DISABLED
        )
        runtime = AutomationRuntime(
            scheduler=None,
            discovery_provider=self.make_provider(),
        )

        runtime.run_cycle()

        self.assertEqual(Envio.objects.count(), 0)
        self.assertEqual(MensagemWhatsApp.objects.count(), 0)
        self.assertTrue(
            LogSistema.objects.filter(
                description__contains="provedor de WhatsApp está desativado"
            ).exists()
        )

    def test_cycle_selects_at_most_one_offer(self):
        first_product = self.add_verified_link()
        second_candidate = OfferCandidate(
            external_id="TEST-AUTOMATION-2",
            title="Segundo produto",
            image_url="https://example.com/automation-image-2.jpg",
            original_url="https://example.com/automation-product-2",
            category="Teste",
            original_price=Decimal("200.00"),
            promotional_price=Decimal("100.00"),
            starts_at=self.now - timedelta(minutes=2),
            ends_at=self.now + timedelta(hours=1),
            is_available=True,
        )
        second_product = create_product(
            name=second_candidate.title,
            external_id=second_candidate.external_id,
            product_url=second_candidate.original_url,
        )
        for product in (first_product, second_product):
            LinkAfiliado.objects.create(
                product=product,
                original_url=product.product_url,
                affiliate_url=f"https://example.com/affiliate-{product.pk}",
                validation_status=LinkAfiliado.ValidationStatus.VALID,
            )
        runtime = AutomationRuntime(
            scheduler=None,
            discovery_provider=self.make_provider([self.candidate, second_candidate]),
        )

        runtime.run_cycle()

        self.assertEqual(Envio.objects.count(), 1)
        self.assertEqual(Envio.objects.get().product, second_product)

    def test_cycle_requires_validated_affiliate_link(self):
        product = create_product(
            name=self.candidate.title,
            external_id=self.candidate.external_id,
            product_url=self.candidate.original_url,
        )
        LinkAfiliado.objects.create(
            product=product,
            original_url=product.product_url,
            affiliate_url="https://example.com/pending-affiliate",
            validation_status=LinkAfiliado.ValidationStatus.PENDING,
        )
        runtime = AutomationRuntime(
            scheduler=None,
            discovery_provider=self.make_provider(),
        )

        runtime.run_cycle()

        self.assertEqual(Envio.objects.count(), 0)
        self.assertEqual(MensagemWhatsApp.objects.count(), 0)

    def test_cycle_does_not_repeat_a_product_sent_through_another_offer(self):
        product = self.add_verified_link()
        previous_offer = create_offer(
            product,
            start_date=self.now - timedelta(days=2),
            end_date=self.now - timedelta(days=1),
            status=Oferta.Status.UNAVAILABLE,
        )
        Envio.objects.create(
            product=product,
            offer=previous_offer,
            message="Envio anterior",
            destination="Grupo de ofertas",
            status=Envio.Status.SENT,
        )
        runtime = AutomationRuntime(
            scheduler=None,
            discovery_provider=self.make_provider(),
        )

        runtime.run_cycle()

        self.assertEqual(Oferta.objects.filter(product=product).count(), 2)
        self.assertEqual(Envio.objects.filter(product=product).count(), 1)

    def test_cycle_skips_expired_offer(self):
        expired_candidate = OfferCandidate(
            **{
                **self.candidate.__dict__,
                "ends_at": self.now - timedelta(seconds=1),
            }
        )
        self.add_verified_link()
        runtime = AutomationRuntime(
            scheduler=None,
            discovery_provider=self.make_provider([expired_candidate]),
        )

        runtime.run_cycle()

        self.assertEqual(Envio.objects.count(), 0)
        self.assertEqual(Oferta.objects.get().status, Oferta.Status.UNAVAILABLE)

    def test_cycle_stops_when_paused_during_discovery(self):
        def pause_robot():
            configuration = Configuracao.current()
            configuration.status = Configuracao.Status.PAUSED
            configuration.save(update_fields=["status", "updated_at"])

        runtime = AutomationRuntime(
            scheduler=None,
            discovery_provider=self.make_provider(on_discover=pause_robot),
        )

        runtime.run_cycle()

        self.assertEqual(Oferta.objects.count(), 0)
        self.assertEqual(Envio.objects.count(), 0)
        self.assertTrue(
            LogSistema.objects.filter(description__contains="Ciclo interrompido").exists()
        )

    def test_cycle_cancels_pending_delivery_when_paused_before_sending(self):
        self.add_verified_link()

        class TestDeliveryProvider:
            is_authorized = True
            is_connected = True

            def __init__(inner_self):
                inner_self.sent_messages = []

            def supports_destination(inner_self, destination):
                return True

            def send(inner_self, destination, message):
                inner_self.sent_messages.append((destination, message))
                return "unexpected-send"

        provider = TestDeliveryProvider()
        Configuracao.objects.filter(pk=self.configuration.pk).update(
            whatsapp_provider=Configuracao.WhatsAppProvider.OFFICIAL_API
        )
        original_prepare = prepare_offer_message

        def prepare_then_pause(*args, **kwargs):
            result = original_prepare(*args, **kwargs)
            Configuracao.objects.filter(pk=self.configuration.pk).update(
                status=Configuracao.Status.PAUSED
            )
            return result

        runtime = AutomationRuntime(
            scheduler=None,
            discovery_provider=self.make_provider(),
            delivery_provider=provider,
        )

        with (
            patch("dashboard.automation.is_official_delivery_ready", return_value=True),
            patch("dashboard.automation.prepare_offer_message", side_effect=prepare_then_pause),
        ):
            runtime.run_cycle()

        self.assertEqual(Envio.objects.get().status, Envio.Status.CANCELLED)
        self.assertEqual(provider.sent_messages, [])

    def test_cycle_records_unavailable_discovery_provider(self):
        runtime = AutomationRuntime(scheduler=None)

        runtime.run_cycle()

        self.assertIsNotNone(Configuracao.current().last_execution)
        self.assertEqual(Oferta.objects.count(), 0)
        self.assertTrue(
            LogSistema.objects.filter(
                event_type=LogSistema.EventType.OFFER_SEARCH,
                severity=LogSistema.Severity.ERROR,
                description__contains="campanhas LIGHTNING",
            ).exists()
        )

    def test_cycle_is_skipped_outside_configured_operating_hours(self):
        configuration = Configuracao.current()
        configuration.operating_hours_enabled = True
        configuration.operating_start_time = time(9, 0)
        configuration.operating_end_time = time(17, 0)
        configuration.save(
            update_fields=[
                "operating_hours_enabled",
                "operating_start_time",
                "operating_end_time",
                "updated_at",
            ]
        )
        provider = self.make_provider()
        runtime = AutomationRuntime(scheduler=None, discovery_provider=provider)
        early_morning = timezone.make_aware(datetime(2026, 11, 26, 2, 0))

        with patch("dashboard.automation.timezone.now", return_value=early_morning):
            runtime.run_cycle()

        self.assertEqual(Oferta.objects.count(), 0)
        self.assertTrue(
            LogSistema.objects.filter(
                description__contains="fora do horário de funcionamento",
            ).exists()
        )

    def test_authorized_connected_provider_is_required_for_sending(self):
        self.add_verified_link()

        class AuthorizedProvider:
            is_authorized = True
            is_connected = True
            sent_messages = []

            def supports_destination(self, destination):
                return destination == "Grupo de ofertas"

            def send(self, destination, message):
                self.sent_messages.append((destination, message))
                return "provider-message-123"

        provider = AuthorizedProvider()
        configuration = Configuracao.current()
        configuration.whatsapp_provider = Configuracao.WhatsAppProvider.OFFICIAL_API
        with patch("dashboard.whatsapp.get_delivery_provider", return_value=provider):
            configuration.save(update_fields=["whatsapp_provider", "updated_at"])
        runtime = AutomationRuntime(
            scheduler=None,
            discovery_provider=self.make_provider(),
            delivery_provider=provider,
        )

        runtime.run_cycle()

        envio = Envio.objects.get()
        self.assertEqual(envio.status, Envio.Status.SENT)
        self.assertEqual(envio.external_id, "provider-message-123")
        self.assertEqual(MensagemWhatsApp.objects.get().status, MensagemWhatsApp.Status.SENT)
        self.assertEqual(len(provider.sent_messages), 1)

    def test_delivery_provider_failure_is_recorded_and_not_marked_sent(self):
        self.add_verified_link()

        class FailingProvider:
            is_authorized = True
            is_connected = True

            def supports_destination(self, destination):
                return True

            def send(self, destination, message):
                raise WhatsAppDeliveryUnavailable("Falha controlada pelo teste.")

        provider = FailingProvider()
        configuration = Configuracao.current()
        configuration.whatsapp_provider = Configuracao.WhatsAppProvider.OFFICIAL_API
        with patch("dashboard.whatsapp.get_delivery_provider", return_value=provider):
            configuration.save(update_fields=["whatsapp_provider", "updated_at"])
        runtime = AutomationRuntime(
            scheduler=None,
            discovery_provider=self.make_provider(),
            delivery_provider=provider,
        )

        runtime.run_cycle()
        runtime.run_cycle()

        self.assertEqual(Envio.objects.get().status, Envio.Status.FAILED)
        self.assertEqual(MensagemWhatsApp.objects.get().status, MensagemWhatsApp.Status.FAILED)
        self.assertEqual(Envio.objects.filter(status=Envio.Status.SENT).count(), 0)
        self.assertTrue(
            LogSistema.objects.filter(
                severity=LogSistema.Severity.ERROR,
                description__contains="Falha controlada",
            ).exists()
        )


class AutomationSchedulerTests(TestCase):
    def test_pause_resume_and_stop_update_real_scheduler_jobs(self):
        scheduler = BackgroundScheduler()
        scheduler.start(paused=True)
        runtime = AutomationRuntime(scheduler=scheduler)
        configuration = Configuracao.current()
        try:
            configuration.status = Configuracao.Status.RUNNING
            configuration.run_now_requested_at = timezone.now()
            configuration.save(
                update_fields=["status", "run_now_requested_at", "updated_at"]
            )
            runtime.sync_control()

            first_job = scheduler.get_job(CYCLE_JOB_ID)
            self.assertIsNotNone(first_job)
            self.assertEqual(first_job.trigger.interval, timedelta(minutes=10))
            self.assertIsNotNone(Configuracao.current().next_execution)
            self.assertIsNotNone(scheduler.get_job(MANUAL_JOB_ID))
            self.assertIsNone(Configuracao.current().run_now_requested_at)

            configuration = Configuracao.current()
            configuration.status = Configuracao.Status.PAUSED
            configuration.save(update_fields=["status", "updated_at"])
            runtime.sync_control()
            self.assertIsNone(scheduler.get_job(CYCLE_JOB_ID))
            self.assertIsNone(scheduler.get_job(MANUAL_JOB_ID))
            self.assertIsNone(Configuracao.current().next_execution)

            configuration = Configuracao.current()
            configuration.status = Configuracao.Status.RUNNING
            configuration.save(update_fields=["status", "updated_at"])
            runtime.sync_control()
            resumed_job = scheduler.get_job(CYCLE_JOB_ID)
            self.assertIsNotNone(resumed_job)
            self.assertGreater(resumed_job.next_run_time, timezone.now())

            configuration = Configuracao.current()
            configuration.status = Configuracao.Status.STOPPED
            configuration.save(update_fields=["status", "updated_at"])
            runtime.sync_control()
            self.assertIsNone(scheduler.get_job(CYCLE_JOB_ID))
        finally:
            scheduler.shutdown(wait=False)

    def test_worker_command_clears_heartbeat_on_graceful_shutdown(self):
        with (
            patch(
                "dashboard.management.commands.run_automation.AutomationProcessLock"
            ) as lock_class,
            patch(
                "dashboard.management.commands.run_automation.BlockingScheduler"
            ) as scheduler_class,
        ):
            lock = lock_class.return_value
            lock.acquire.return_value = True
            scheduler = scheduler_class.return_value
            scheduler.running = False
            scheduler.start.side_effect = KeyboardInterrupt

            call_command("run_automation")

        self.assertIsNone(Configuracao.current().worker_heartbeat)
        lock.release.assert_called_once()

    def test_quick_pause_resume_discards_stale_cycle_generation(self):
        scheduler = BackgroundScheduler()
        scheduler.start(paused=True)
        runtime = AutomationRuntime(scheduler=scheduler)
        configuration = Configuracao.current()
        old_control_time = timezone.now()
        try:
            configuration.status = Configuracao.Status.RUNNING
            configuration.control_changed_at = old_control_time
            configuration.save(update_fields=["status", "control_changed_at", "updated_at"])
            runtime.sync_control()

            configuration.status = Configuracao.Status.PAUSED
            configuration.control_changed_at = old_control_time + timedelta(microseconds=1)
            configuration.save(update_fields=["status", "control_changed_at", "updated_at"])
            configuration.status = Configuracao.Status.RUNNING
            configuration.control_changed_at = old_control_time + timedelta(microseconds=2)
            configuration.save(update_fields=["status", "control_changed_at", "updated_at"])
            runtime.sync_control()

            job = scheduler.get_job(CYCLE_JOB_ID)
            self.assertIsNotNone(job)
            self.assertEqual(job.args[0], configuration.control_changed_at)
            runtime.run_cycle(old_control_time)
            self.assertIsNone(Configuracao.current().last_execution)
        finally:
            scheduler.shutdown(wait=False)


class BusinessMigrationTests(TransactionTestCase):
    migrate_from = [("dashboard", "0001_initial")]
    migrate_to = [("dashboard", "0002_business_domain")]

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_from)
        old_apps = executor.loader.project_state(self.migrate_from).apps

        old_apps.get_model("dashboard", "RobotSettings").objects.create(
            send_interval_minutes=7,
            whatsapp_destination="+5511888888888",
        )
        old_apps.get_model("dashboard", "RobotState").objects.create(
            status="paused",
            last_execution=timezone.now(),
        )
        old_apps.get_model("dashboard", "Offer").objects.create(
            name="Oferta existente",
            image_url="https://example.com/imagem.jpg",
            original_price=Decimal("200.00"),
            promotional_price=Decimal("150.00"),
            product_url="https://example.com/produto-legado",
            affiliate_url="https://example.com/link-legado",
            status="valid",
        )
        old_apps.get_model("dashboard", "ActivityLog").objects.create(
            level="error",
            message="Erro legado preservado",
        )

        executor = MigrationExecutor(connection)
        executor.migrate(self.migrate_to)
        self.new_apps = executor.loader.project_state(self.migrate_to).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_migration_preserves_legacy_offer_configuration_and_log(self):
        Product = self.new_apps.get_model("dashboard", "Produto")
        Offer = self.new_apps.get_model("dashboard", "Oferta")
        AffiliateLink = self.new_apps.get_model("dashboard", "LinkAfiliado")
        Configuration = self.new_apps.get_model("dashboard", "Configuracao")
        SystemLog = self.new_apps.get_model("dashboard", "LogSistema")

        product = Product.objects.get(product_url="https://example.com/produto-legado")
        offer = Offer.objects.get(product_id=product.pk)
        configuration = Configuration.objects.get(pk=1)

        self.assertEqual(product.name, "Oferta existente")
        self.assertIsNone(product.external_id)
        self.assertEqual(offer.status, "available")
        self.assertEqual(offer.discount_percent, Decimal("25.00"))
        self.assertEqual(AffiliateLink.objects.count(), 1)
        self.assertEqual(configuration.send_interval_minutes, 7)
        self.assertEqual(configuration.status, "paused")
        self.assertEqual(SystemLog.objects.get().description, "Erro legado preservado")
