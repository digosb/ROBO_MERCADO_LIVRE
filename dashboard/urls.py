from django.urls import path

from . import views


app_name = "dashboard"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("ofertas/", views.offers, name="offers"),
    path("afiliados/", views.affiliates, name="affiliates"),
    path("whatsapp/", views.whatsapp, name="whatsapp"),
    path("historico/", views.history, name="history"),
    path("configuracoes/", views.settings_page, name="settings"),
]
