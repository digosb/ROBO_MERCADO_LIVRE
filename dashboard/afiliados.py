from dataclasses import dataclass
from datetime import date, datetime, time
from typing import NoReturn
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.utils import timezone

from .models import LinkAfiliado, LogSistema, Produto


_validate_url = URLValidator(schemes=["http", "https"])


class AffiliateLinkImportError(Exception):
    """Falha ao importar um link informado pelo usuário."""


@dataclass(frozen=True)
class AffiliateLinkImportResult:
    link: LinkAfiliado
    created: bool


def _validate_web_url(value: str, field_name: str) -> None:
    try:
        _validate_url(value)
        parsed_url = urlsplit(value)
    except (ValidationError, ValueError) as error:
        raise AffiliateLinkImportError(
            f"Informe uma URL HTTP ou HTTPS válida para {field_name}."
        ) from error

    if parsed_url.username or parsed_url.password:
        raise AffiliateLinkImportError(
            f"A URL de {field_name} não pode conter credenciais."
        )


def _log_import(description: str, severity: str) -> None:
    LogSistema.objects.create(
        event_type=LogSistema.EventType.AFFILIATE_IMPORT,
        description=description,
        severity=severity,
    )


def _reject_import(message: str, cause: Exception | None = None) -> NoReturn:
    _log_import(
        f"Importação manual de link afiliado rejeitada: {message}",
        LogSistema.Severity.WARNING,
    )
    error = AffiliateLinkImportError(message)
    if cause is not None:
        raise error from cause
    raise error


def import_manual_affiliate_link(
    original_url: str,
    affiliate_url: str,
    generated_date: str = "",
) -> AffiliateLinkImportResult:
    original_url = original_url.strip()
    affiliate_url = affiliate_url.strip()

    try:
        _validate_web_url(original_url, "URL original")
        _validate_web_url(affiliate_url, "URL afiliada")
    except AffiliateLinkImportError as error:
        _reject_import(str(error), error)

    generated_at = None
    if generated_date:
        try:
            generated_day = date.fromisoformat(generated_date)
        except ValueError as error:
            _reject_import("A data de geração informada é inválida.", error)
        generated_at = timezone.make_aware(
            datetime.combine(generated_day, time.min),
            timezone.get_current_timezone(),
        )

    try:
        with transaction.atomic():
            product = Produto.objects.get(product_url=original_url)
            link, created = LinkAfiliado.objects.get_or_create(
                product=product,
                affiliate_url=affiliate_url,
                defaults={
                    "original_url": original_url,
                    "generated_at": generated_at,
                    "validation_status": LinkAfiliado.ValidationStatus.PENDING,
                },
            )
            result_description = (
                "Link afiliado importado"
                if created
                else "Link afiliado já registrado; duplicata não criada"
            )
            _log_import(
                f"{result_description} para o produto {product.external_id or product.pk}. "
                "Validação de afiliação permanece pendente.",
                LogSistema.Severity.INFO,
            )
    except Produto.DoesNotExist as error:
        _reject_import(
            "Não existe produto cadastrado com a URL original informada.",
            error,
        )
    except ValidationError as error:
        _reject_import("; ".join(error.messages), error)

    return AffiliateLinkImportResult(link=link, created=created)
