import json

from django.contrib import messages
from django.db.models import Q
from django.http import JsonResponse
from django.core.paginator import Paginator
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .afiliados import AffiliateLinkImportError, import_manual_affiliate_link
from .automation import is_worker_online
from .forms import ConfiguracaoForm, EnvioFilterForm
from .models import (
    Configuracao,
    Envio,
    LinkAfiliado,
    LogSistema,
    MensagemWhatsApp,
    Oferta,
)
from .reporting import build_report_metrics
from .whatsapp import WhatsAppPreparationError, prepare_offer_message, validate_destination


def _dashboard_context():
    configuration = Configuracao.current()
    report_metrics = build_report_metrics()
    metrics = {
        "offers_found": report_metrics["offers_found"],
        "valid_offers": report_metrics["offers_approved"],
        "affiliate_links": LinkAfiliado.objects.count(),
        "messages_sent": report_metrics["messages_sent"],
        "errors": report_metrics["failures"],
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
            "notifications": (
                LogSistema.objects.filter(
                    severity__in=[
                        LogSistema.Severity.WARNING,
                        LogSistema.Severity.ERROR,
                        LogSistema.Severity.CRITICAL,
                    ]
                )[:5]
                if settings.notifications_enabled
                else []
            ),
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
    history_filter = EnvioFilterForm(request.GET)
    sendings = Envio.objects.select_related("product", "offer")
    if history_filter.is_valid():
        filters = history_filter.cleaned_data
        if filters["date_from"]:
            sendings = sendings.filter(created_at__date__gte=filters["date_from"])
        if filters["date_to"]:
            sendings = sendings.filter(created_at__date__lte=filters["date_to"])
        if filters["product"]:
            sendings = sendings.filter(
                Q(product_name_snapshot__icontains=filters["product"])
                | Q(product__name__icontains=filters["product"])
            )
        if filters["status"]:
            sendings = sendings.filter(status=filters["status"])
        if filters["offer_type"]:
            sendings = sendings.filter(offer__promotion_type=filters["offer_type"])
    else:
        sendings = sendings.none()

    sendings_page = Paginator(sendings, 25).get_page(request.GET.get("page"))
    query_params = request.GET.copy()
    query_params.pop("page", None)
    return render(
        request,
        "dashboard/history.html",
        {
            "active_page": "history",
            "history_filter": history_filter,
            "sendings_page": sendings_page,
            "filter_query": query_params.urlencode(),
            "events": LogSistema.objects.all()[:50],
        },
    )


def reports(request):
    return render(
        request,
        "dashboard/reports.html",
        {
            "active_page": "reports",
            "report": build_report_metrics(),
        },
    )


def settings_page(request):
    settings = Configuracao.current()
    if request.method == "POST":
        form = ConfiguracaoForm(request.POST, instance=settings)
        if form.is_valid():
            form.save()
            messages.success(request, "Configurações salvas.")
            return redirect("dashboard:settings")
        messages.error(request, "Revise os campos destacados; as configurações não foram alteradas.")
        return render(
            request,
            "dashboard/settings.html",
            {
                "active_page": "settings",
                "settings": settings,
                "settings_form": form,
            },
        )
    return render(
        request,
        "dashboard/settings.html",
        {
            "active_page": "settings",
            "settings": settings,
            "settings_form": ConfiguracaoForm(instance=settings),
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
