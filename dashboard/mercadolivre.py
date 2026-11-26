from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from django.core.cache import cache


CACHE_TIMEOUT_SECONDS = 60
REQUEST_COOLDOWN_SECONDS = 10
_CACHE_MISS = object()

UNAVAILABLE_SOURCE_MESSAGE = (
    "A busca geral de ofertas relâmpago não está disponível: a documentação oficial "
    "consultada só confirma a API de campanhas LIGHTNING dos próprios vendedores "
    "convidados. Nenhuma oferta foi consultada ou criada."
)


class OfferDiscoveryError(Exception):
    """Erro conhecido na descoberta de ofertas."""


class OfferDiscoveryUnavailable(OfferDiscoveryError):
    """Não há provedor autorizado disponível para a descoberta solicitada."""


class OfferDiscoveryRateLimited(OfferDiscoveryError):
    """Uma consulta recente ainda está dentro do intervalo mínimo local."""


class OfferProviderError(OfferDiscoveryError):
    """O provedor configurado não conseguiu concluir a consulta."""


@dataclass(frozen=True)
class OfferCandidate:
    """Contrato interno do provedor; não representa o payload de uma API específica."""

    external_id: str
    title: str
    image_url: str
    original_url: str
    category: str
    original_price: Decimal
    promotional_price: Decimal
    starts_at: datetime
    ends_at: datetime | None
    is_available: bool


class OfferProvider(Protocol):
    cache_namespace: str
    is_available: bool

    def discover(self) -> list[OfferCandidate]:
        """Retorna ofertas normalizadas e verificadas pelo provedor."""
        ...


class MarketplaceFlashDealsProvider:
    cache_namespace = "marketplace-flash-deals"
    is_available = False

    def discover(self) -> list[OfferCandidate]:
        raise OfferDiscoveryUnavailable(UNAVAILABLE_SOURCE_MESSAGE)


def discover_flash_deals(
    provider: OfferProvider | None = None,
) -> list[OfferCandidate]:
    provider = provider or MarketplaceFlashDealsProvider()
    if not provider.is_available:
        raise OfferDiscoveryUnavailable(UNAVAILABLE_SOURCE_MESSAGE)

    cache_key = f"mercadolivre:flash-deals:v1:{provider.cache_namespace}"
    cached_offers = cache.get(cache_key, _CACHE_MISS)
    if cached_offers is not _CACHE_MISS:
        return cached_offers

    cooldown_key = f"{cache_key}:cooldown"
    if not cache.add(cooldown_key, True, timeout=REQUEST_COOLDOWN_SECONDS):
        raise OfferDiscoveryRateLimited(
            "A busca foi limitada para evitar consultas repetidas. Tente novamente em alguns segundos."
        )

    offers = provider.discover()
    cache.set(cache_key, offers, timeout=CACHE_TIMEOUT_SECONDS)
    return offers
