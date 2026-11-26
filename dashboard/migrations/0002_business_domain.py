from decimal import Decimal, ROUND_HALF_UP

import django.core.validators
import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models
from django.db.models import Q


def migrate_existing_records(apps, schema_editor):
    database = schema_editor.connection.alias
    Product = apps.get_model("dashboard", "Produto")
    Offer = apps.get_model("dashboard", "Oferta")
    AffiliateLink = apps.get_model("dashboard", "LinkAfiliado")
    Configuration = apps.get_model("dashboard", "Configuracao")
    RobotState = apps.get_model("dashboard", "RobotState")

    status_map = {
        "valid": "available",
        "invalid": "unavailable",
        "pending": "unknown",
    }
    for offer in Offer.objects.using(database).all().iterator():
        product, _ = Product.objects.using(database).get_or_create(
            product_url=offer.product_url,
            defaults={
                "name": offer.name,
                "image_url": offer.image_url,
                "created_at": offer.identified_at,
            },
        )
        discount = Decimal("0.00")
        if offer.original_price > 0:
            calculated = (
                (offer.original_price - offer.promotional_price)
                * Decimal("100")
                / offer.original_price
            )
            discount = max(
                Decimal("0.00"),
                min(Decimal("100.00"), calculated.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            )
        Offer.objects.using(database).filter(pk=offer.pk).update(
            product_id=product.pk,
            discount_percent=discount,
            start_date=offer.identified_at,
            status=status_map.get(offer.status, "unknown"),
        )
        if offer.affiliate_url:
            AffiliateLink.objects.using(database).get_or_create(
                product_id=product.pk,
                affiliate_url=offer.affiliate_url,
                defaults={
                    "original_url": offer.product_url,
                    "validation_status": "pending",
                    "generated_at": offer.identified_at,
                },
            )

    for old_state in RobotState.objects.using(database).all().iterator():
        configuration, _ = Configuration.objects.using(database).get_or_create(pk=old_state.pk)
        Configuration.objects.using(database).filter(pk=configuration.pk).update(
            status=old_state.status,
            last_execution=old_state.last_execution,
        )


def restore_legacy_records(apps, schema_editor):
    database = schema_editor.connection.alias
    Offer = apps.get_model("dashboard", "Oferta")
    AffiliateLink = apps.get_model("dashboard", "LinkAfiliado")
    Configuration = apps.get_model("dashboard", "Configuracao")
    RobotState = apps.get_model("dashboard", "RobotState")
    reverse_status_map = {
        "available": "valid",
        "unavailable": "invalid",
        "unknown": "pending",
    }
    for offer in Offer.objects.using(database).select_related("product").iterator():
        affiliate_link = (
            AffiliateLink.objects.using(database)
            .filter(product_id=offer.product_id)
            .first()
        )
        Offer.objects.using(database).filter(pk=offer.pk).update(
            name=offer.product.name,
            image_url=offer.product.image_url,
            product_url=offer.product.product_url,
            affiliate_url=affiliate_link.affiliate_url if affiliate_link else "",
            status=reverse_status_map.get(offer.status, "pending"),
        )

    for configuration in Configuration.objects.using(database).all().iterator():
        RobotState.objects.using(database).update_or_create(
            pk=configuration.pk,
            defaults={
                "status": configuration.status,
                "last_execution": configuration.last_execution,
            },
        )
class Migration(migrations.Migration):
    dependencies = [
        ("dashboard", "0001_initial"),
    ]

    operations = [
        migrations.RenameModel(old_name="RobotSettings", new_name="Configuracao"),
        migrations.RenameModel(old_name="ActivityLog", new_name="LogSistema"),
        migrations.RenameField(
            model_name="logsistema",
            old_name="level",
            new_name="severity",
        ),
        migrations.RenameField(
            model_name="logsistema",
            old_name="message",
            new_name="description",
        ),
        migrations.RenameField(
            model_name="logsistema",
            old_name="created_at",
            new_name="occurred_at",
        ),
        migrations.AlterField(
            model_name="logsistema",
            name="description",
            field=models.TextField(max_length=1000, verbose_name="Descrição"),
        ),
        migrations.AlterField(
            model_name="logsistema",
            name="occurred_at",
            field=models.DateTimeField(
                default=django.utils.timezone.now,
                verbose_name="Data e horário",
            ),
        ),
        migrations.AddField(
            model_name="logsistema",
            name="event_type",
            field=models.CharField(
                choices=[
                    ("robot_started", "Robô iniciado"),
                    ("robot_paused", "Robô pausado"),
                    ("robot_stopped", "Robô parado"),
                    ("offer_search", "Busca de ofertas"),
                    ("whatsapp_preview", "Prévia de WhatsApp"),
                    ("send", "Envio"),
                    ("system", "Sistema"),
                ],
                default="system",
                max_length=24,
                verbose_name="Tipo de evento",
            ),
        ),
        migrations.AlterField(
            model_name="logsistema",
            name="severity",
            field=models.CharField(
                choices=[
                    ("debug", "Depuração"),
                    ("info", "Informação"),
                    ("warning", "Aviso"),
                    ("error", "Erro"),
                    ("critical", "Crítico"),
                ],
                default="info",
                max_length=10,
                verbose_name="Nível de gravidade",
            ),
        ),
        migrations.RenameModel(old_name="Offer", new_name="Oferta"),
        migrations.CreateModel(
            name="Produto",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=300, verbose_name="Nome")),
                (
                    "external_id",
                    models.CharField(
                        blank=True,
                        max_length=128,
                        null=True,
                        unique=True,
                        verbose_name="Identificador externo",
                    ),
                ),
                ("image_url", models.URLField(blank=True, verbose_name="Imagem")),
                ("product_url", models.URLField(unique=True, verbose_name="URL original")),
                ("category", models.CharField(blank=True, max_length=180, verbose_name="Categoria")),
                (
                    "created_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now,
                        verbose_name="Data de cadastro",
                    ),
                ),
            ],
            options={
                "verbose_name": "Produto",
                "verbose_name_plural": "Produtos",
                "ordering": ["name"],
            },
        ),
        migrations.AddField(
            model_name="configuracao",
            name="max_sends_per_day",
            field=models.PositiveIntegerField(
                default=50,
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(10000),
                ],
                verbose_name="Quantidade máxima de envios por dia",
            ),
        ),
        migrations.AddField(
            model_name="configuracao",
            name="execution_settings",
            field=models.JSONField(
                blank=True,
                default=dict,
                verbose_name="Configurações de execução",
            ),
        ),
        migrations.AddField(
            model_name="configuracao",
            name="status",
            field=models.CharField(
                choices=[
                    ("stopped", "Parado"),
                    ("running", "Em execução (demonstração)"),
                    ("paused", "Pausado"),
                ],
                default="stopped",
                max_length=12,
                verbose_name="Status do robô",
            ),
        ),
        migrations.AddField(
            model_name="configuracao",
            name="last_execution",
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name="Última execução",
            ),
        ),
        migrations.AlterModelOptions(
            name="configuracao",
            options={"verbose_name": "Configuração", "verbose_name_plural": "Configurações"},
        ),
        migrations.AddField(
            model_name="oferta",
            name="product",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="offers",
                to="dashboard.produto",
                verbose_name="Produto",
            ),
        ),
        migrations.AddField(
            model_name="oferta",
            name="discount_percent",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                max_digits=5,
                validators=[
                    django.core.validators.MinValueValidator(Decimal("0.00")),
                    django.core.validators.MaxValueValidator(Decimal("100.00")),
                ],
                verbose_name="Percentual de desconto",
            ),
        ),
        migrations.AddField(
            model_name="oferta",
            name="promotion_type",
            field=models.CharField(
                choices=[
                    ("discount", "Desconto"),
                    ("coupon", "Cupom"),
                    ("cashback", "Cashback"),
                    ("bundle", "Oferta combinada"),
                    ("other", "Outro"),
                ],
                default="other",
                max_length=12,
                verbose_name="Tipo de promoção",
            ),
        ),
        migrations.AddField(
            model_name="oferta",
            name="start_date",
            field=models.DateTimeField(
                default=django.utils.timezone.now,
                verbose_name="Data de início",
            ),
        ),
        migrations.AddField(
            model_name="oferta",
            name="end_date",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Data de término"),
        ),
        migrations.AddField(
            model_name="oferta",
            name="last_checked_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name="Data da última verificação",
            ),
        ),
        migrations.AlterField(
            model_name="oferta",
            name="status",
            field=models.CharField(
                choices=[
                    ("available", "Disponível"),
                    ("unavailable", "Indisponível"),
                    ("unknown", "Não verificada"),
                ],
                default="unknown",
                max_length=12,
                verbose_name="Status de disponibilidade",
            ),
        ),
        migrations.AlterField(
            model_name="oferta",
            name="original_price",
            field=models.DecimalField(
                decimal_places=2,
                max_digits=12,
                validators=[django.core.validators.MinValueValidator(Decimal("0.01"))],
                verbose_name="Preço original",
            ),
        ),
        migrations.AlterField(
            model_name="oferta",
            name="promotional_price",
            field=models.DecimalField(
                decimal_places=2,
                max_digits=12,
                validators=[django.core.validators.MinValueValidator(Decimal("0.01"))],
                verbose_name="Preço promocional",
            ),
        ),
        migrations.AlterField(
            model_name="oferta",
            name="identified_at",
            field=models.DateTimeField(
                auto_now_add=True,
                verbose_name="Data de identificação",
            ),
        ),
        migrations.CreateModel(
            name="LinkAfiliado",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("original_url", models.URLField(verbose_name="URL original")),
                ("affiliate_url", models.URLField(verbose_name="URL afiliada")),
                (
                    "validation_status",
                    models.CharField(
                        choices=[
                            ("pending", "Pendente"),
                            ("valid", "Válido"),
                            ("invalid", "Inválido"),
                        ],
                        default="pending",
                        max_length=10,
                        verbose_name="Status de validação",
                    ),
                ),
                (
                    "generated_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now,
                        verbose_name="Data de geração",
                    ),
                ),
                (
                    "product",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="affiliate_links",
                        to="dashboard.produto",
                        verbose_name="Produto",
                    ),
                ),
            ],
            options={
                "verbose_name": "Link de afiliado",
                "verbose_name_plural": "Links de afiliados",
                "ordering": ["-generated_at"],
            },
        ),
        migrations.CreateModel(
            name="Envio",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("message", models.TextField(max_length=2000, verbose_name="Mensagem")),
                ("destination", models.CharField(max_length=120, verbose_name="Destino")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pendente"),
                            ("sent", "Enviado"),
                            ("failed", "Falhou"),
                            ("cancelled", "Cancelado"),
                        ],
                        default="pending",
                        max_length=10,
                        verbose_name="Status",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now,
                        verbose_name="Data e horário",
                    ),
                ),
                (
                    "external_id",
                    models.CharField(
                        blank=True,
                        max_length=200,
                        verbose_name="Identificador externo",
                    ),
                ),
                (
                    "offer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="sendings",
                        to="dashboard.oferta",
                        verbose_name="Oferta",
                    ),
                ),
                (
                    "product",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="sendings",
                        to="dashboard.produto",
                        verbose_name="Produto",
                    ),
                ),
            ],
            options={
                "verbose_name": "Envio",
                "verbose_name_plural": "Envios",
                "ordering": ["-created_at"],
            },
        ),
        migrations.RunPython(migrate_existing_records, restore_legacy_records),
        migrations.RemoveField(model_name="oferta", name="name"),
        migrations.RemoveField(model_name="oferta", name="image_url"),
        migrations.RemoveField(model_name="oferta", name="product_url"),
        migrations.RemoveField(model_name="oferta", name="affiliate_url"),
        migrations.AlterField(
            model_name="oferta",
            name="product",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="offers",
                to="dashboard.produto",
                verbose_name="Produto",
            ),
        ),
        migrations.AddConstraint(
            model_name="linkafiliado",
            constraint=models.UniqueConstraint(
                fields=("product", "affiliate_url"),
                name="unique_affiliate_url_per_product",
            ),
        ),
        migrations.AddConstraint(
            model_name="envio",
            constraint=models.UniqueConstraint(
                condition=Q(("status__in", ["pending", "sent"])),
                fields=("offer",),
                name="unique_active_send_per_offer",
            ),
        ),
        migrations.DeleteModel(name="RobotState"),
        migrations.AlterModelOptions(
            name="logsistema",
            options={
                "ordering": ["-occurred_at"],
                "verbose_name": "Log do sistema",
                "verbose_name_plural": "Logs do sistema",
            },
        ),
    ]
