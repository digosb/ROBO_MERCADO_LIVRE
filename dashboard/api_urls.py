from django.urls import path

from . import views


urlpatterns = [
    path("status/", views.api_status, name="api_status"),
    path("robot/<str:action>/", views.robot_action, name="robot_action"),
    path("whatsapp/prepare/", views.prepare_whatsapp_message, name="whatsapp_prepare"),
]
