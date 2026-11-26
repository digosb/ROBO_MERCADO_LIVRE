from django.contrib import admin

from .models import Configuracao, Envio, LinkAfiliado, LogSistema, Oferta, Produto


@admin.register(Produto)
class ProdutoAdmin(admin.ModelAdmin):
    list_display = ("name", "external_id", "category", "created_at")
    list_filter = ("category", "created_at")
    search_fields = ("name", "external_id", "product_url")


@admin.register(Oferta)
class OfertaAdmin(admin.ModelAdmin):
    list_display = (
        "product",
        "original_price",
        "promotional_price",
        "discount_percent",
        "status",
        "last_checked_at",
    )
    list_filter = ("status", "promotion_type", "start_date")
    search_fields = ("product__name", "product__external_id")
    readonly_fields = ("discount_percent", "identified_at")


@admin.register(LinkAfiliado)
class LinkAfiliadoAdmin(admin.ModelAdmin):
    list_display = ("product", "validation_status", "generated_at")
    list_filter = ("validation_status", "generated_at")
    search_fields = ("product__name", "affiliate_url")


@admin.register(Envio)
class EnvioAdmin(admin.ModelAdmin):
    list_display = ("product", "offer", "destination", "status", "created_at", "external_id")
    list_filter = ("status", "created_at")
    search_fields = ("product__name", "offer__product__external_id", "destination", "external_id")
    readonly_fields = ("created_at",)


@admin.register(Configuracao)
class ConfiguracaoAdmin(admin.ModelAdmin):
    list_display = ("status", "send_interval_minutes", "max_sends_per_day", "updated_at")
    readonly_fields = ("updated_at",)


@admin.register(LogSistema)
class LogSistemaAdmin(admin.ModelAdmin):
    list_display = ("event_type", "severity", "description", "occurred_at")
    list_filter = ("event_type", "severity", "occurred_at")
    search_fields = ("description",)
    readonly_fields = ("event_type", "severity", "description", "occurred_at")
