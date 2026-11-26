from decimal import Decimal

from django.db.models import Count, Q

from .models import Envio, LinkAfiliado, LogSistema, Oferta


def build_report_metrics():
    offers = Oferta.objects.aggregate(
        found=Count("id"),
        approved=Count("id", filter=Q(status=Oferta.Status.AVAILABLE)),
    )
    sent_count = Envio.objects.filter(status=Envio.Status.SENT).count()
    failed_sends = Envio.objects.filter(status=Envio.Status.FAILED).count()
    system_failures = LogSistema.objects.filter(
        severity__in=[LogSistema.Severity.ERROR, LogSistema.Severity.CRITICAL],
    ).exclude(event_type=LogSistema.EventType.SEND).count()
    completed_attempts = sent_count + failed_sends
    success_percentage = (
        (Decimal(sent_count) * Decimal("100") / Decimal(completed_attempts)).quantize(
            Decimal("0.01")
        )
        if completed_attempts
        else Decimal("0.00")
    )

    return {
        "offers_found": offers["found"],
        "offers_approved": offers["approved"],
        "valid_affiliate_links": LinkAfiliado.objects.filter(
            validation_status=LinkAfiliado.ValidationStatus.VALID,
        ).count(),
        "messages_sent": sent_count,
        "failures": failed_sends + system_failures,
        "failed_sends": failed_sends,
        "system_failures": system_failures,
        "success_percentage": success_percentage,
        "completed_attempts": completed_attempts,
    }
