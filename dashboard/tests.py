import json
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

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
    Oferta,
    Produto,
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
    def test_dashboard_starts_with_empty_real_metrics_and_demo_label(self):
        response = self.client.get(reverse("dashboard:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "0")
        self.assertContains(response, "demonstração")
        self.assertEqual(response.context["metrics"]["offers_found"], 0)
        self.assertEqual(response.context["metrics"]["messages_sent"], 0)

    def test_navigation_pages_render(self):
        for name in (
            "dashboard:offers",
            "dashboard:affiliates",
            "dashboard:whatsapp",
            "dashboard:history",
            "dashboard:settings",
        ):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)


class RobotEndpointTests(TestCase):
    def test_robot_actions_change_persisted_status(self):
        for action, expected in (
            ("start", Configuracao.Status.RUNNING),
            ("pause", Configuracao.Status.PAUSED),
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

    def test_search_reports_unavailable_source_without_creating_offers(self):
        response = self.client.post(reverse("robot_action", args=["search"]))

        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json()["success"])
        self.assertIsNone(Configuracao.current().last_execution)
        self.assertEqual(Oferta.objects.count(), 0)
        self.assertIn("campanhas LIGHTNING", response.json()["message"])
        log = LogSistema.objects.get()
        self.assertEqual(log.event_type, LogSistema.EventType.OFFER_SEARCH)
        self.assertEqual(log.severity, LogSistema.Severity.WARNING)

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


class ConfigurationTests(TestCase):
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
                    {
                        "send_interval_minutes": invalid,
                        "max_sends_per_day": "12",
                    },
                )
                self.assertEqual(Configuracao.current().send_interval_minutes, 10)
                self.assertEqual(Configuracao.current().max_sends_per_day, 50)

        self.client.post(
            url,
            {"send_interval_minutes": "15", "max_sends_per_day": "12"},
        )
        self.assertEqual(Configuracao.current().send_interval_minutes, 15)
        self.assertEqual(Configuracao.current().max_sends_per_day, 12)

        self.client.post(
            url,
            {"send_interval_minutes": "15", "max_sends_per_day": "10001"},
        )
        self.assertEqual(Configuracao.current().max_sends_per_day, 12)

    def test_whatsapp_preview_requires_destination_and_does_not_send(self):
        url = reverse("whatsapp_prepare")
        body = json.dumps({"offer_name": "Oferta de teste"})

        missing_destination = self.client.post(
            url, body, content_type="application/json"
        )
        self.assertEqual(missing_destination.status_code, 400)

        configuration = Configuracao.current()
        configuration.whatsapp_destination = "+5511999999999"
        configuration.save()
        response = self.client.post(url, body, content_type="application/json")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Nenhuma mensagem foi enviada", response.json()["message"])
        self.assertEqual(response.json()["destination"], configuration.whatsapp_destination)
        self.assertEqual(Envio.objects.count(), 0)
        self.assertEqual(LogSistema.objects.count(), 1)

    def test_whatsapp_preview_rejects_malformed_and_non_object_json(self):
        configuration = Configuracao.current()
        configuration.whatsapp_destination = "+5511999999999"
        configuration.save()
        url = reverse("whatsapp_prepare")

        malformed = self.client.post(url, "{", content_type="application/json")
        non_object = self.client.post(url, "[]", content_type="application/json")

        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(non_object.status_code, 400)

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
