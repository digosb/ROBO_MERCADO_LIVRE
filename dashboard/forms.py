from django import forms

from .models import Configuracao, Envio, Oferta
from .whatsapp import (
    WhatsAppPreparationError,
    is_official_delivery_ready,
    validate_destination,
)


class EnvioFilterForm(forms.Form):
    date_from = forms.DateField(
        required=False,
        label="De",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    date_to = forms.DateField(
        required=False,
        label="Até",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    product = forms.CharField(
        required=False,
        max_length=100,
        label="Produto",
        widget=forms.TextInput(attrs={"placeholder": "Nome do produto"}),
    )
    status = forms.ChoiceField(
        required=False,
        label="Status",
        choices=[("", "Todos os status"), *Envio.Status.choices],
    )
    offer_type = forms.ChoiceField(
        required=False,
        label="Tipo de oferta",
        choices=[("", "Todos os tipos"), *Oferta.PromotionType.choices],
    )

    def clean(self):
        cleaned_data = super().clean()
        date_from = cleaned_data.get("date_from")
        date_to = cleaned_data.get("date_to")
        if date_from and date_to and date_from > date_to:
            self.add_error("date_to", "A data final deve ser igual ou posterior à inicial.")
        return cleaned_data


class ConfiguracaoForm(forms.ModelForm):
    class Meta:
        model = Configuracao
        fields = (
            "send_interval_minutes",
            "max_sends_per_day",
            "operating_hours_enabled",
            "operating_start_time",
            "operating_end_time",
            "whatsapp_destination",
            "notifications_enabled",
            "offer_provider",
            "affiliate_provider",
            "whatsapp_provider",
        )
        widgets = {
            "send_interval_minutes": forms.NumberInput(attrs={"min": 1, "max": 1440}),
            "max_sends_per_day": forms.NumberInput(attrs={"min": 1, "max": 10000}),
            "operating_start_time": forms.TimeInput(attrs={"type": "time"}),
            "operating_end_time": forms.TimeInput(attrs={"type": "time"}),
            "whatsapp_destination": forms.TextInput(
                attrs={"placeholder": "Rótulo local do destino"}
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        destination = (
            self.data.get("whatsapp_destination", "")
            if self.is_bound
            else self.instance.whatsapp_destination
        )
        try:
            destination = validate_destination(destination) if destination else ""
        except WhatsAppPreparationError:
            destination = ""
        if not is_official_delivery_ready(destination):
            self.fields["whatsapp_provider"].choices = [
                (
                    Configuracao.WhatsAppProvider.DISABLED,
                    Configuracao.WhatsAppProvider.DISABLED.label,
                ),
                (
                    Configuracao.WhatsAppProvider.PREPARATION_ONLY,
                    Configuracao.WhatsAppProvider.PREPARATION_ONLY.label,
                )
            ]

    def clean_whatsapp_destination(self):
        destination = self.cleaned_data.get("whatsapp_destination", "")
        if not destination:
            return ""
        try:
            return validate_destination(destination)
        except WhatsAppPreparationError as error:
            raise forms.ValidationError(str(error)) from error
