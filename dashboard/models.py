from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone


class Produto(models.Model):
    name = models.CharField(max_length=300, verbose_name="Nome")
    external_id = models.CharField(
        max_length=128,
        unique=True,
        null=True,
        blank=True,
        verbose_name="Identificador externo",
    )
    image_url = models.URLField(blank=True, verbose_name="Imagem")
    product_url = models.URLField(unique=True, verbose_name="URL original")
    category = models.CharField(max_length=180, blank=True, verbose_name="Categoria")
    created_at = models.DateTimeField(default=timezone.now, verbose_name="Data de cadastro")

    class Meta:
        ordering = ["name"]
        verbose_name = "Produto"
        verbose_name_plural = "Produtos"

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.external_id is not None:
            external_id = self.external_id.strip().casefold()
            self.external_id = external_id or None

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class Oferta(models.Model):
    class Status(models.TextChoices):
        AVAILABLE = "available", "Disponível"
        UNAVAILABLE = "unavailable", "Indisponível"
        UNKNOWN = "unknown", "Não verificada"

    class PromotionType(models.TextChoices):
        DISCOUNT = "discount", "Desconto"
        COUPON = "coupon", "Cupom"
        CASHBACK = "cashback", "Cashback"
        BUNDLE = "bundle", "Oferta combinada"
        OTHER = "other", "Outro"

    product = models.ForeignKey(
        Produto,
        on_delete=models.PROTECT,
        related_name="offers",
        verbose_name="Produto",
    )
    original_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        verbose_name="Preço original",
    )
    promotional_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        verbose_name="Preço promocional",
    )
    discount_percent = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[
            MinValueValidator(Decimal("0.00")),
            MaxValueValidator(Decimal("100.00")),
        ],
        verbose_name="Percentual de desconto",
    )
    promotion_type = models.CharField(
        max_length=12,
        choices=PromotionType.choices,
        default=PromotionType.OTHER,
        verbose_name="Tipo de promoção",
    )
    start_date = models.DateTimeField(default=timezone.now, verbose_name="Data de início")
    end_date = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Data de término",
    )
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.UNKNOWN,
        verbose_name="Status de disponibilidade",
    )
    last_checked_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Data da última verificação",
    )
    identified_at = models.DateTimeField(auto_now_add=True, verbose_name="Data de identificação")

    class Meta:
        ordering = ["-identified_at"]
        verbose_name = "Oferta"
        verbose_name_plural = "Ofertas"

    def __str__(self):
        return f"{self.product.name} — R$ {self.promotional_price}"

    def clean(self):
        super().clean()
        if self.original_price is None or self.promotional_price is None:
            return
        if self.original_price <= 0:
            raise ValidationError({"original_price": "O preço original deve ser maior que zero."})
        if self.promotional_price <= 0:
            raise ValidationError(
                {"promotional_price": "O preço promocional deve ser maior que zero."}
            )
        if self.promotional_price > self.original_price:
            raise ValidationError(
                {"promotional_price": "O preço promocional não pode superar o preço original."}
            )
        discount = (
            (self.original_price - self.promotional_price)
            * Decimal("100")
            / self.original_price
        )
        self.discount_percent = discount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if self.end_date and self.start_date and self.end_date < self.start_date:
            raise ValidationError(
                {"end_date": "A data de término não pode anteceder a data de início."}
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class LinkAfiliado(models.Model):
    class ValidationStatus(models.TextChoices):
        PENDING = "pending", "Pendente"
        VALID = "valid", "Válido"
        INVALID = "invalid", "Inválido"

    product = models.ForeignKey(
        Produto,
        on_delete=models.PROTECT,
        related_name="affiliate_links",
        verbose_name="Produto",
    )
    original_url = models.URLField(verbose_name="URL original")
    affiliate_url = models.URLField(verbose_name="URL afiliada")
    validation_status = models.CharField(
        max_length=10,
        choices=ValidationStatus.choices,
        default=ValidationStatus.PENDING,
        verbose_name="Status de validação",
    )
    generated_at = models.DateTimeField(default=timezone.now, verbose_name="Data de geração")

    class Meta:
        ordering = ["-generated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["product", "affiliate_url"],
                name="unique_affiliate_url_per_product",
            )
        ]
        verbose_name = "Link de afiliado"
        verbose_name_plural = "Links de afiliados"

    def __str__(self):
        return f"{self.product.name} — {self.affiliate_url}"

    def clean(self):
        super().clean()
        if self.product_id and self.original_url and self.product.product_url != self.original_url:
            raise ValidationError(
                {"original_url": "A URL original deve corresponder ao produto selecionado."}
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class Envio(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pendente"
        SENT = "sent", "Enviado"
        FAILED = "failed", "Falhou"
        CANCELLED = "cancelled", "Cancelado"

    product = models.ForeignKey(
        Produto,
        on_delete=models.PROTECT,
        related_name="sendings",
        verbose_name="Produto",
    )
    offer = models.ForeignKey(
        Oferta,
        on_delete=models.PROTECT,
        related_name="sendings",
        verbose_name="Oferta",
    )
    message = models.TextField(max_length=2000, verbose_name="Mensagem")
    destination = models.CharField(max_length=120, verbose_name="Destino")
    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
        verbose_name="Status",
    )
    created_at = models.DateTimeField(default=timezone.now, verbose_name="Data e horário")
    external_id = models.CharField(
        max_length=200,
        blank=True,
        verbose_name="Identificador externo",
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["offer"],
                condition=Q(status__in=["pending", "sent"]),
                name="unique_active_send_per_offer",
            )
        ]
        verbose_name = "Envio"
        verbose_name_plural = "Envios"

    def __str__(self):
        return f"{self.product.name} — {self.get_status_display()}"

    def clean(self):
        super().clean()
        if not self.offer_id or not self.product_id:
            return
        if self.offer.product_id != self.product_id:
            raise ValidationError({"product": "O produto deve ser o mesmo da oferta selecionada."})
        if self.status not in (self.Status.PENDING, self.Status.SENT) or self.created_at is None:
            return

        active_statuses = (self.Status.PENDING, self.Status.SENT)
        active_send = Envio.objects.filter(offer_id=self.offer_id, status__in=active_statuses)
        created_at = self.created_at
        send_date = (
            timezone.localtime(created_at).date()
            if timezone.is_aware(created_at)
            else created_at.date()
        )
        daily_sends = Envio.objects.filter(
            status__in=active_statuses,
            created_at__date=send_date,
        )
        if self.pk:
            active_send = active_send.exclude(pk=self.pk)
            daily_sends = daily_sends.exclude(pk=self.pk)
        if active_send.exists():
            raise ValidationError(
                {"offer": "Já existe um envio pendente ou concluído para esta oferta."}
            )

        configuration = Configuracao.current()
        if daily_sends.count() >= configuration.max_sends_per_day:
            raise ValidationError(
                {"status": "O limite diário de envios configurado já foi atingido."}
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class Configuracao(models.Model):
    class Status(models.TextChoices):
        STOPPED = "stopped", "Parado"
        RUNNING = "running", "Em execução (demonstração)"
        PAUSED = "paused", "Pausado"

    send_interval_minutes = models.PositiveIntegerField(
        default=10,
        validators=[MinValueValidator(1), MaxValueValidator(1440)],
        verbose_name="Intervalo de envio (minutos)",
    )
    max_sends_per_day = models.PositiveIntegerField(
        default=50,
        validators=[MinValueValidator(1), MaxValueValidator(10000)],
        verbose_name="Quantidade máxima de envios por dia",
    )
    status = models.CharField(
        max_length=12,
        choices=Status.choices,
        default=Status.STOPPED,
        verbose_name="Status do robô",
    )
    execution_settings = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Configurações de execução",
    )
    whatsapp_destination = models.CharField(
        max_length=80,
        blank=True,
        verbose_name="Destino do WhatsApp",
    )
    last_execution = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Última execução",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Configuração"
        verbose_name_plural = "Configurações"

    def __str__(self):
        return "Configuração do robô"

    @classmethod
    def current(cls):
        configuration, _ = cls.objects.get_or_create(pk=1)
        return configuration

    def clean(self):
        super().clean()
        if not isinstance(self.execution_settings, dict):
            raise ValidationError(
                {"execution_settings": "As configurações de execução devem ser um objeto JSON."}
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class LogSistema(models.Model):
    class EventType(models.TextChoices):
        ROBOT_STARTED = "robot_started", "Robô iniciado"
        ROBOT_PAUSED = "robot_paused", "Robô pausado"
        ROBOT_STOPPED = "robot_stopped", "Robô parado"
        OFFER_SEARCH = "offer_search", "Busca de ofertas"
        WHATSAPP_PREVIEW = "whatsapp_preview", "Prévia de WhatsApp"
        SEND = "send", "Envio"
        SYSTEM = "system", "Sistema"

    class Severity(models.TextChoices):
        DEBUG = "debug", "Depuração"
        INFO = "info", "Informação"
        WARNING = "warning", "Aviso"
        ERROR = "error", "Erro"
        CRITICAL = "critical", "Crítico"

    event_type = models.CharField(
        max_length=24,
        choices=EventType.choices,
        default=EventType.SYSTEM,
        verbose_name="Tipo de evento",
    )
    description = models.TextField(max_length=1000, verbose_name="Descrição")
    occurred_at = models.DateTimeField(default=timezone.now, verbose_name="Data e horário")
    severity = models.CharField(
        max_length=10,
        choices=Severity.choices,
        default=Severity.INFO,
        verbose_name="Nível de gravidade",
    )

    class Meta:
        ordering = ["-occurred_at"]
        verbose_name = "Log do sistema"
        verbose_name_plural = "Logs do sistema"

    def __str__(self):
        return self.description

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


# Aliases preserve imports used by integrations built against the first dashboard version.
Product = Produto
Offer = Oferta
AffiliateLink = LinkAfiliado
Sending = Envio
RobotSettings = Configuracao
RobotState = Configuracao
ActivityLog = LogSistema
