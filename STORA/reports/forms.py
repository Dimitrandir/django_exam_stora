from django import forms

from STORA.products.models import Suppliers, Category


class ReportPeriodForm(forms.Form):
    start_date = forms.DateField(
        label='From date',
        widget=forms.DateInput(attrs={'type': 'date'}),
    )
    end_date = forms.DateField(
        label='To date',
        widget=forms.DateInput(attrs={'type': 'date'}),
    )
    supplier = forms.ModelChoiceField(
        label='Supplier',
        queryset=Suppliers.objects.all(),
        required=False,
        empty_label='All suppliers',
        # Rendered as a plain <select> by default -- deliveries_report.html
        # swaps this for a search+popup-free picker in JS, same pattern as
        # the delivery form's own supplier field (see supplier_search).
        widget=forms.HiddenInput(),
    )
    category = forms.ModelChoiceField(
        label='Category',
        queryset=Category.objects.all(),
        required=False,
        empty_label='All categories',
    )

    def clean(self):
        cleaned_data = super().clean()
        start_date = cleaned_data.get('start_date')
        end_date = cleaned_data.get('end_date')

        if start_date and end_date and start_date > end_date:
            raise forms.ValidationError('Start date cannot be later than end date.')

        return cleaned_data