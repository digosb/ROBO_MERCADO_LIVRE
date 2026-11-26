import json

from django.contrib import messages
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .afiliados import AffiliateLinkImportError, import_manual_affiliate_link
from .automation import is_worker_online
from .models import (
    Configuracao,
    Envio,
    LinkAfiliado,
    LogSistema,
    MensagemWhatsApp,
    Oferta,
)
from .whatsapp import WhatsAppPreparationError, prepare_offer_message, validate_destination


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
            "worker_online": is_worker_online(state),
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
        if not destination:
            settings.whatsapp_destination = ""
            settings.save(update_fields=["whatsapp_destination", "updated_at"])
            messages.success(request, "Identificação do destino removida.")
        else:
            try:
                destination = validate_destination(destination)
            except WhatsAppPreparationError as error:
                messages.error(request, str(error))
            else:
                settings.whatsapp_destination = destination
                settings.save(update_fields=["whatsapp_destination", "updated_at"])
                messages.success(
                    request,
                    "Identificação do destino salva. Isso não valida o grupo nem habilita envios.",
                )
        return redirect("dashboard:whatsapp")
    return render(
        request,
        "dashboard/whatsapp.html",
        {
            "active_page": "whatsapp",
            "settings": settings,
            "offers": Oferta.objects.select_related("product").all(),
            "whatsapp_messages": MensagemWhatsApp.objects.select_related(
                "offer__product"
            )[:50],
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
            "next_execution": state.next_execution.isoformat() if state.next_execution else None,
            "worker_online": is_worker_online(state),
            "automatic_sending_enabled": False,
            "send_interval_minutes": settings.send_interval_minutes,
            "max_sends_per_day": settings.max_sends_per_day,
            "metrics": metrics,
        }
    )


@require_POST
def robot_action(_request, action):
    if action not in {"start", "pause", "resume", "stop", "search"}:
        return JsonResponse({"success": False, "message": "Ação desconhecida."}, status=404)

    state = Configuracao.current()
    worker_online = is_worker_online(state)
    if action in {"start", "resume"} and not worker_online:
        return JsonResponse(
            {
                "success": False,
                "message": (
                    "O processo do motor está desconectado. Inicie "
                    "run_automation antes de controlar o agendamento."
                ),
                "code": "worker_offline",
            },
            status=503,
        )
    if action == "pause" and state.status != Configuracao.Status.RUNNING:
        return JsonResponse(
            {"success": False, "message": "O robô só pode ser pausado quando está em execução."},
            status=409,
        )
    if action == "search":
        if state.status != Configuracao.Status.RUNNING:
            return JsonResponse(
                {"success": False, "message": "Inicie o robô antes de solicitar uma busca."},
                status=409,
            )
        if not worker_online:
            return JsonResponse(
                {"success": False, "message": "O processo do motor está desconectado."},
                status=503,
            )
        state.run_now_requested_at = timezone.now()
        state.save(update_fields=["run_now_requested_at", "updated_at"])
        message = (
            "Busca adicionada à fila do worker. O provedor padrão está indisponível; "
            "nenhuma oferta será simulada."
        )
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
                "last_execution": (
                    state.last_execution.isoformat() if state.last_execution else None
                ),
                "next_execution": state.next_execution.isoformat() if state.next_execution else None,
            }
        )

    action_labels = {
        "start": (
            "Ciclos do motor iniciados. A fonte de ofertas e o envio oficial seguem "
            "indisponíveis; nenhum disparo automático será feito."
        ),
        "pause": "Robô pausado; nenhum novo ciclo será iniciado.",
        "resume": "Robô retomado; um novo intervalo será contado sem recuperar ciclos atrasados.",
        "stop": "Robô parado; ciclos pendentes foram descartados.",
    }
    action_event_types = {
        "start": LogSistema.EventType.ROBOT_STARTED,
        "pause": LogSistema.EventType.ROBOT_PAUSED,
        "resume": LogSistema.EventType.ROBOT_STARTED,
        "stop": LogSistema.EventType.ROBOT_STOPPED,
    }
    if action == "start":
        if state.status != Configuracao.Status.STOPPED:
            return JsonResponse(
                {"success": False, "message": "O robô já está ativo ou pausado; use retomar."},
                status=409,
            )
        state.status = Configuracao.Status.RUNNING
        state.control_changed_at = timezone.now()
        state.run_now_requested_at = None
        state.next_execution = None
        state.save(
            update_fields=[
                "status",
                "control_changed_at",
                "run_now_requested_at",
                "next_execution",
                "updated_at",
            ]
        )
    elif action == "pause":
        state.status = Configuracao.Status.PAUSED
        state.control_changed_at = timezone.now()
        state.run_now_requested_at = None
        state.next_execution = None
        state.save(
            update_fields=[
                "status",
                "control_changed_at",
                "run_now_requested_at",
                "next_execution",
                "updated_at",
            ]
        )
    elif action == "resume":
        if state.status != Configuracao.Status.PAUSED:
            return JsonResponse(
                {"success": False, "message": "O robô só pode ser retomado quando está pausado."},
                status=409,
            )
        state.status = Configuracao.Status.RUNNING
        state.control_changed_at = timezone.now()
        state.run_now_requested_at = None
        state.next_execution = None
        state.save(
            update_fields=[
                "status",
                "control_changed_at",
                "run_now_requested_at",
                "next_execution",
                "updated_at",
            ]
        )
    elif action == "stop":
        state.status = Configuracao.Status.STOPPED
        state.control_changed_at = timezone.now()
        state.run_now_requested_at = None
        state.next_execution = None
        state.save(
            update_fields=[
                "status",
                "control_changed_at",
                "run_now_requested_at",
                "next_execution",
                "updated_at",
            ]
        )
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
            "next_execution": state.next_execution.isoformat() if state.next_execution else None,
            "worker_online": worker_online,
        }
    )


@require_POST
def prepare_whatsapp_message(request):
    settings = Configuracao.current()
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "message": "Requisição JSON inválida."}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({"success": False, "message": "O corpo JSON deve ser um objeto."}, status=400)
    raw_offer_id = payload.get("offer_id")
    if isinstance(raw_offer_id, bool) or not isinstance(raw_offer_id, (str, int)):
        return JsonResponse(
            {"success": False, "message": "Selecione uma oferta cadastrada."},
            status=400,
        )
    try:
        offer_id = int(raw_offer_id)
    except (TypeError, ValueError):
        return JsonResponse(
            {"success": False, "message": "Selecione uma oferta cadastrada."},
            status=400,
        )
    is_test = payload.get("is_test", False)
    if not isinstance(is_test, bool):
        return JsonResponse(
            {"success": False, "message": "O campo de teste deve ser verdadeiro ou falso."},
            status=400,
        )
    try:
        result = prepare_offer_message(
            offer_id=offer_id,
            destination=settings.whatsapp_destination,
            is_test=is_test,
        )
    except WhatsAppPreparationError as error:
        return JsonResponse({"success": False, "message": str(error)}, status=400)
    return JsonResponse(
        {
            "success": True,
            "message": (
                "Mensagem preparada localmente. Copie e compartilhe manualmente; "
                "nenhuma mensagem foi enviada."
            ),
            "preview": result.record.message,
            "destination": result.record.destination,
            "offer_name": result.record.offer.product.name,
            "status": result.record.get_status_display(),
            "created_at": timezone.localtime(result.record.created_at).strftime(
                "%d/%m/%Y %H:%M"
            ),
            "created": result.created,
        },
    )
