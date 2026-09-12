from django import forms
from django.forms import inlineformset_factory

from STORA.sales.models import SaleItems, SaleAttributes

class SaleForms(forms.ModelForm):
    class Meta:
        model = SaleAttributes
        fields = ['cashier', 'payment_method', 'amount_paid', 'card_amount', 'change_due']
        labels = {'cashier': 'Cashier'}
        widgets = {
            # Set by the checkout modal's JS -- not rendered as visible
            # form controls here.
            'payment_method': forms.HiddenInput(),
            'amount_paid': forms.HiddenInput(),
            'card_amount': forms.HiddenInput(),
            'change_due': forms.HiddenInput(),
        }

    def __init__(self, *args, current_user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if current_user is not None:
            self.fields['cashier'].disabled = True
            self.fields['cashier'].initial = current_user


class SaleItemForm(forms.ModelForm):
    product_code = forms.CharField(
        label='Product Code / Barcode',
        required=False,
        widget=forms.TextInput(attrs={'class': 'product-code-input', 'placeholder': 'Scan or type code'})
    )

    product_name = forms.CharField(
        label='Product Name',
        required=False,
        disabled=True,
        widget=forms.TextInput(attrs={'class': 'product-name-input'})
    )

    unit_price = forms.DecimalField(
        label='Unit Price',
        required=False,
        disabled=True,
        decimal_places=2,
        max_digits=9,
        widget=forms.NumberInput(attrs={'class': 'unit-price-input'})
    )

    line_total = forms.DecimalField(
        label='Line Total',
        required=False,
        disabled=True,
        decimal_places=2,
        max_digits=9,
        widget=forms.NumberInput(attrs={'class': 'line-total-input'})
    )

    class Meta:
        model = SaleItems
        fields = ['sale_item', 'sale_quantity', 'price_at_sale', 'total_price_row']
        widgets = {
            'sale_item': forms.HiddenInput(),
            # min/step start as whole-piece defaults (1/1) -- the template's
            # JS (applyQuantityConstraints) switches them to 0.001/0.001 the
            # moment a weight product is picked, since a plain HTML number
            # input with step=1 rejects a fractional value like 0.350 at
            # submit time even though the JS already wrote it in.
            # HTML5 step validation is relative to `min`, not to 0 -- min=
            # "0.001" with step="1" would make 1, 2, 3... all INVALID (only
            # 0.001, 1.001, 2.001... pass), which silently blocked every
            # ordinary whole-number sale until this was min="1" instead.
            'sale_quantity': forms.NumberInput(attrs={'min': '1', 'step': '1', 'class': 'quantity-input'}),
            'price_at_sale': forms.HiddenInput(),
            'total_price_row': forms.HiddenInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['sale_quantity'].required = True
        self.fields['sale_item'].required = True


class BaseSaleItemFormSet(forms.BaseInlineFormSet):
    def clean(self):
        super().clean()

        has_valid_item = False

        for form in self.forms:
            if not hasattr(form, 'cleaned_data'):
                continue

            if form.cleaned_data.get('DELETE'):
                continue

            sale_item = form.cleaned_data.get('sale_item')
            sale_quantity = form.cleaned_data.get('sale_quantity')

            if sale_item and sale_quantity:
                has_valid_item = True
                break

        if not has_valid_item:
            raise forms.ValidationError('You must add at least one sale item.')


SaleItemFormSet = inlineformset_factory(
    SaleAttributes,
    SaleItems,
    form=SaleItemForm,
    formset=BaseSaleItemFormSet,
    extra=1,
    can_delete=True,
)


