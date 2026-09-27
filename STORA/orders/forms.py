from django import forms

from STORA.products.models import Suppliers
from STORA.orders.models import OrderAttributes


class OrderPickerForm(forms.Form):
    """Supplier + supplier-position + sales period -- picks which products
    show up as candidates on the compose screen (order_new). All three are
    required here (unlike ReportPeriodForm's optional "All suppliers"):
    there's no meaningful "every supplier at once" candidate list for an
    order that's always addressed to exactly one supplier."""

    supplier = forms.ModelChoiceField(
        label='Supplier',
        queryset=Suppliers.objects.all(),
        # Rendered as a plain <select> by default -- order_new.html swaps
        # this for the same search+popup picker as the delivery form's own
        # supplier field (see supplier_search).
        widget=forms.HiddenInput(),
    )
    # Checkboxes, not a single <select> -- asked for live so all three
    # ranked-supplier tiers can be pulled into one order at once instead
    # of running the compose screen three separate times.
    supplier_position = forms.MultipleChoiceField(
        label='Supplier position', choices=OrderAttributes.POSITION_CHOICES,
        initial=[OrderAttributes.POSITION_PRIMARY], widget=forms.CheckboxSelectMultiple,
    )
    start_date = forms.DateField(label='From date', widget=forms.DateInput(attrs={'type': 'date'}))
    end_date = forms.DateField(label='To date', widget=forms.DateInput(attrs={'type': 'date'}))

    def clean(self):
        cleaned_data = super().clean()
        start_date = cleaned_data.get('start_date')
        end_date = cleaned_data.get('end_date')
        if start_date and end_date and start_date > end_date:
            raise forms.ValidationError('Start date cannot be later than end date.')
        return cleaned_data
