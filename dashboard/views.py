import json

from django.contrib import messages
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .afiliados import AffiliateLinkImportError, import_manual_affiliate_link
from .mercadolivre import (
    OfferDiscoveryRateLimited,
    OfferDiscoveryUnavailable,
    OfferProviderError,
    discover_flash_deals,
)
from .models import Configuracao, Envio, LinkAfiliado, LogSistema, Oferta


def _dashboard_context():
    configuration = Configuracao.current()
    offers = Oferta.objects.aggregate(
        total=Count("id"),
        valid=Count("id", filter=Q(status=Oferta.Status.AVAILABLE)),
    )
    metrics = {
        "offers_found": offers["total"],
        "valid_offers": offers["valid"],
        "affiliate_links": LinkAfiliado.objects.count(),
        "messages_sent": Envio.objects.filter(status=Envio.Status.SENT).count(),
        "errors": LogSistema.objects.filter(severity=LogSistema.Severity.ERROR).count(),
    }
    return configuration, configuration, metrics


def dashboard(request):
    settings, state, metrics = _dashboard_context()
    recent_offers = Oferta.objects.select_related("product")[:5]
    return render(
        request,
        "dashboard/dashboard.html",
        {
            "active_page": "dashboard",
            "settings": settings,
            "robot_state": state,
            "metrics": metrics,
            "recent_offers": recent_offers,
        },
    )


def offers(request):
    return render(
        request,
        "dashboard/offers.html",
        {
            "active_page": "offers",
            "offers": Oferta.objects.select_related("product").all(),
        },
    )


def affiliates(request):
    if request.method == "POST":
        try:
            result = import_manual_affiliate_link(
                original_url=request.POST.get("original_url", ""),
                affiliate_url=request.POST.get("affiliate_url", ""),
                generated_date=request.POST.get("generated_date", "").strip(),
            )
        except AffiliateLinkImportError as error:
            messages.error(request, str(error))
        else:
            if result.created:
                messages.success(
                    request,
                    "Link importado. A validação de afiliação permanece pendente.",
                )
            else:
                messages.warning(request, "Esse link já estava registrado para o produto.")
        return redirect("dashboard:affiliates")

    affiliate_links = LinkAfiliado.objects.select_related("product")
    return render(
        request,
        "dashboard/affiliates.html",
        {
            "active_page": "affiliates",
            "affiliate_links": affiliate_links,
        },
    )


def whatsapp(request):
    settings = Configuracao.current()
    if request.method == "POST":
        destination = request.POST.get("destination", "").strip()
        if len(destination) > 80:
            messages.error(request, "O destino deve ter no máximo 80 caracteres.")
        else:
            settings.whatsapp_destination = destination
            settings.save(update_fields=["whatsapp_destination", "updated_at"])
            messages.success(request, "Destino do WhatsApp atualizado.")
        return redirect("dashboard:whatsapp")
    return render(
        request,
        "dashboard/whatsapp.html",
        {
            "active_page": "whatsapp",
            "settings": settings,
        },
    )


def history(request):
    return render(
        request,
        "dashboard/history.html",
        {
            "active_page": "history",
            "events": LogSistema.objects.all()[:100],
        },
    )


def settings_page(request):
    settings = Configuracao.current()
    if request.method == "POST":
        raw_interval = request.POST.get("send_interval_minutes", "").strip()
        raw_max_sends = request.POST.get("max_sends_per_day", "").strip()
        try:
            interval = int(raw_interval)
            max_sends = (
                int(raw_max_sends)
                if raw_max_sends
                else settings.max_sends_per_day
            )
        except ValueError:
            messages.error(
                request,
                "Informe o intervalo e o limite diário como números inteiros válidos.",
            )
        else:
            if not 1 <= interval <= 1440:
                messages.error(request, "O intervalo deve estar entre 1 e 1440 minutos.")
            elif not 1 <= max_sends <= 10000:
                messages.error(
                    request,
                    "O limite diário deve estar entre 1 e 10000 envios.",
                )
            else:
                settings.send_interval_minutes = interval
                settings.max_sends_per_day = max_sends
                settings.save(
                    update_fields=[
                        "send_interval_minutes",
                        "max_sends_per_day",
                        "updated_at",
                    ]
                )
                messages.success(request, "Configurações salvas.")
        return redirect("dashboard:settings")
    return render(
        request,
        "dashboard/settings.html",
        {
            "active_page": "settings",
            "settings": settings,
        },
    )


@require_GET
def api_status(_request):
    settings, state, metrics = _dashboard_context()
    return JsonResponse(
        {
            "status": state.status,
            "status_label": state.get_status_display(),
            "last_execution": state.last_execution.isoformat() if state.last_execution else None,
            "next_execution": None,
            "send_interval_minutes": settings.send_interval_minutes,
            "max_sends_per_day": settings.max_sends_per_day,
            "metrics": metrics,
        }
    )


@require_POST
def robot_action(_request, action):
    if action not in {"start", "pause", "stop", "search"}:
        return JsonResponse({"success": False, "message": "Ação desconhecida."}, status=404)

    state = Configuracao.current()
    if action == "pause" and state.status != Configuracao.Status.RUNNING:
        return JsonResponse(
            {"success": False, "message": "O robô só pode ser pausado quando está em execução."},
            status=409,
        )
    if action == "start" and state.status == Configuracao.Status.RUNNING:
        return JsonResponse({"success": False, "message": "O robô já está em execução."}, status=409)

    if action == "search":
        try:
            offers = discover_flash_deals()
        except OfferDiscoveryUnavailable as error:
            LogSistema.objects.create(
                event_type=LogSistema.EventType.OFFER_SEARCH,
                description=str(error),
                severity=LogSistema.Severity.WARNING,
            )
            return JsonResponse(
                {"success": False, "message": str(error), "code": "source_unavailable"},
                status=503,
            )
        except OfferDiscoveryRateLimited as error:
            LogSistema.objects.create(
                event_type=LogSistema.EventType.OFFER_SEARCH,
                description=str(error),
                severity=LogSistema.Severity.WARNING,
            )
            return JsonResponse(
                {"success": False, "message": str(error), "code": "rate_limited"},
                status=429,
            )
        except OfferProviderError as error:
            LogSistema.objects.create(
                event_type=LogSistema.EventType.OFFER_SEARCH,
                description=str(error),
                severity=LogSistema.Severity.ERROR,
            )
            return JsonResponse(
                {"success": False, "message": str(error), "code": "provider_error"},
                status=502,
            )

        state.last_execution = timezone.now()
        state.save(update_fields=["last_execution", "updated_at"])
        message = f"Busca concluída: {len(offers)} ofertas verificadas pelo provedor."
        LogSistema.objects.create(
            event_type=LogSistema.EventType.OFFER_SEARCH,
            description=message,
        )
        return JsonResponse(
            {
                "success": True,
                "message": message,
                "status": state.status,
                "status_label": state.get_status_display(),
                "last_execution": state.last_execution.isoformat(),
            }
        )

    action_labels = {
        "start": "Robô iniciado em modo de demonstração. Nenhuma integração externa foi acionada.",
        "pause": "Robô pausado.",
        "stop": "Robô parado.",
    }
    action_event_types = {
        "start": LogSistema.EventType.ROBOT_STARTED,
        "pause": LogSistema.EventType.ROBOT_PAUSED,
        "stop": LogSistema.EventType.ROBOT_STOPPED,
    }
    if action == "start":
        state.status = Configuracao.Status.RUNNING
        state.save(update_fields=["status", "updated_at"])
    elif action == "pause":
        state.status = Configuracao.Status.PAUSED
        state.save(update_fields=["status", "updated_at"])
    elif action == "stop":
        state.status = Configuracao.Status.STOPPED
        state.save(update_fields=["status", "updated_at"])
    LogSistema.objects.create(
        event_type=action_event_types[action],
        description=action_labels[action],
    )
    return JsonResponse(
        {
            "success": True,
            "message": action_labels[action],
            "status": state.status,
            "status_label": state.get_status_display(),
            "last_execution": state.last_execution.isoformat() if state.last_execution else None,
        }
    )


@require_POST
def prepare_whatsapp_message(_request):
    settings = Configuracao.current()
    if not settings.whatsapp_destination:
        return JsonResponse(
            {
                "success": False,
                "message": "Configure o destino antes de preparar uma mensagem.",
            },
            status=400,
        )
    try:
        payload = json.loads(_request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "message": "Requisição JSON inválida."}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"success": False, "message": "O corpo JSON deve ser um objeto."}, status=400)
    offer_name = str(payload.get("offer_name", "")).strip()
    if len(offer_name) > 200:
        return JsonResponse(
            {"success": False, "message": "O nome da oferta deve ter até 200 caracteres."},
            status=400,
        )

    message = (
        f"Prévia de demonstração para {offer_name or 'uma oferta'} — "
        "nenhuma mensagem foi enviada."
    )
    LogSistema.objects.create(
        event_type=LogSistema.EventType.WHATSAPP_PREVIEW,
        description=(
            f"Prévia de mensagem preparada para {settings.whatsapp_destination} (não enviada)."
        ),
    )
    return JsonResponse(
        {
            "success": True,
            "message": "Prévia preparada. Nenhuma mensagem foi enviada.",
            "preview": message,
            "destination": settings.whatsapp_destination,
            "method": "Preparação local (demonstração)",
        }
    )
