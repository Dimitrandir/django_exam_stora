from django import forms

from STORA.pricelists.models import PriceList


class PriceListForm(forms.ModelForm):
    class Meta:
        model = PriceList
        fields = ['name', 'priority', 'start_date', 'end_date']
        widgets = {
            'start_date': forms.DateInput(attrs={'type': 'date'}),
            'end_date': forms.DateInput(attrs={'type': 'date'}),
        }
