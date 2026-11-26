from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from typing import Protocol
from urllib.parse import urlsplit

from django.core.validators import URLValidator
from django.core.exceptions import ValidationError
from django.db import transaction

from .models import (
    LinkAfiliado,
    LogSistema,
    MensagemWhatsApp,
    Oferta,
)


class WhatsAppPreparationError(Exception):
    """Falha ao validar ou preparar uma mensagem para WhatsApp."""


class WhatsAppDeliveryUnavailable(Exception):
    """O destino configurado não pode ser alcançado por uma integração autorizada."""


class PreparationProvider(Protocol):
    def prepare(self, offer: Oferta) -> str: ...


class DeliveryProvider(Protocol):
    is_authorized: bool
    is_connected: bool

    def supports_destination(self, destination: str) -> bool: ...

    def send(self, destination: str, message: str) -> str: ...


class ManualPreparationProvider:
    def prepare(self, offer: Oferta, require_validated_link: bool = False) -> str:
        valid_statuses = [LinkAfiliado.ValidationStatus.VALID]
        if not require_validated_link:
            valid_statuses.append(LinkAfiliado.ValidationStatus.PENDING)
        affiliate_link = (
            offer.product.affiliate_links.filter(
                validation_status__in=valid_statuses
            )
            .order_by("-registered_at")
            .first()
        )
        if affiliate_link is None:
            raise WhatsAppPreparationError(
                "Cadastre um link de afiliado para este produto antes de preparar a mensagem."
            )

        return (
            "🔥 OFERTA RELÂMPAGO!\n\n"
            f"📦 {offer.product.name}\n\n"
            f"❌ De: {format_brl(offer.original_price)}\n"
            f"✅ Por: {format_brl(offer.promotional_price)}\n\n"
            f"🔥 Desconto: {format_brl_percent(offer.discount_percent)}%\n\n"
            f"🛒 LINK:\n{affiliate_link.affiliate_url}\n\n"
            "⚠️ Oferta sujeita à disponibilidade e alteração de preço."
        )


@dataclass(frozen=True)
class PreparationResult:
    record: MensagemWhatsApp
    created: bool


def format_brl(value: Decimal) -> str:
    formatted = f"{value:,.2f}"
    return f"R$ {formatted.replace(',', 'X').replace('.', ',').replace('X', '.')}"


def format_brl_percent(value: Decimal) -> str:
    return format_brl(value).removeprefix("R$ ")


def validate_destination(destination: str) -> str:
    if not isinstance(destination, str):
        raise WhatsAppPreparationError("Informe um nome válido para o grupo de destino.")
    destination = destination.strip()
    if not destination:
        raise WhatsAppPreparationError("Configure o nome do grupo de destino.")
    if len(destination) > 80:
        raise WhatsAppPreparationError("O nome do destino deve ter até 80 caracteres.")

    try:
        URLValidator(schemes=["http", "https"])(destination)
    except ValidationError:
        pass
    else:
        raise WhatsAppPreparationError(
            "Informe apenas um rótulo para o grupo. Links de convite não são destinos de envio."
        )

    parsed = urlsplit(destination)
    if parsed.scheme or parsed.netloc or "/" in destination:
        raise WhatsAppPreparationError("O destino deve ser um rótulo, não uma URL.")
    return destination


def _record_failure(
    offer: Oferta | None,
    destination: str,
    error: WhatsAppPreparationError,
    is_test: bool,
) -> None:
    with transaction.atomic():
        MensagemWhatsApp.objects.create(
            offer=offer,
            destination=destination[:80],
            status=MensagemWhatsApp.Status.FAILED,
            is_test=is_test,
            error_detail=str(error)[:500],
        )
        LogSistema.objects.create(
            event_type=LogSistema.EventType.WHATSAPP_PREVIEW,
            description=f"Preparação de mensagem WhatsApp falhou: {error}",
            severity=LogSistema.Severity.WARNING,
        )


def prepare_offer_message(
    offer_id: int,
    destination: str,
    is_test: bool = False,
    require_validated_link: bool = False,
) -> PreparationResult:
    offer = None
    try:
        destination = validate_destination(destination)
        try:
            offer = Oferta.objects.select_related("product").get(pk=offer_id)
        except Oferta.DoesNotExist as error:
            raise WhatsAppPreparationError("A oferta selecionada não existe.") from error

        message = ManualPreparationProvider().prepare(
            offer,
            require_validated_link=require_validated_link,
        )
    except WhatsAppPreparationError as error:
        _record_failure(offer, destination if isinstance(destination, str) else "", error, is_test)
        raise

    with transaction.atomic():
        record, created = MensagemWhatsApp.objects.get_or_create(
            dedupe_key=sha256(
                f"{offer.pk}\0{destination}\0{is_test}\0{message}".encode("utf-8")
            ).hexdigest(),
            defaults={
                "offer": offer,
                "destination": destination,
                "message": message,
                "is_test": is_test,
                "status": MensagemWhatsApp.Status.PREPARED,
            },
        )
        LogSistema.objects.create(
            event_type=LogSistema.EventType.WHATSAPP_PREVIEW,
            description=(
                "Mensagem preparada localmente; não enviada."
                if created
                else "Mensagem repetida; registro preparado existente reutilizado."
            ),
            severity=LogSistema.Severity.INFO if created else LogSistema.Severity.WARNING,
        )

    return PreparationResult(record=record, created=created)


class UnsupportedOfficialDeliveryProvider:
    is_authorized = False
    is_connected = False

    def supports_destination(self, destination: str) -> bool:
        return False

    def send(self, destination: str, message: str) -> str:
        raise WhatsAppDeliveryUnavailable(
            "Envio automatizado indisponível: a Groups API não é compatível com "
            "o número pessoal e o grupo existente configurados."
        )


def get_delivery_provider() -> DeliveryProvider:
    return UnsupportedOfficialDeliveryProvider()


def is_official_delivery_ready(destination: str, provider=None) -> bool:
    provider = provider or get_delivery_provider()
    return bool(
        destination
        and provider.is_authorized
        and provider.is_connected
        and provider.supports_destination(destination)
    )


