from django import forms
from django.forms import inlineformset_factory
from STORA.deliveries.models import DeliveryAttributes, DeliveryItems, DocumentType


class DeliveryForms(forms.ModelForm):
    class Meta:
        model = DeliveryAttributes
        fields = ['receiver', 'supplier', 'time_of_delivery', 'document_type', 'document_number', 'document_date']
        labels = {
            'receiver': 'Receiver',
            'time_of_delivery': 'Delivery Date',
            'document_type': 'Document Type',
            'document_number': 'Document Number',
            'document_date': 'Document Date',
            'supplier': 'Supplier'
        }
        widgets = {
            'document_date': forms.DateInput(attrs={'type': 'date'}),
            # Rendered as a plain <select> by default -- the template swaps
            # this for a search+popup picker in JS, same as `supplier` below.
            'supplier': forms.HiddenInput(),
        }

    def __init__(self, *args, current_user=None, **kwargs):
        super().__init__(*args, **kwargs)
        if current_user is not None:
            self.fields['receiver'].disabled = True
            self.fields['receiver'].initial = current_user


class DocumentTypeForm(forms.ModelForm):
    class Meta:
        model = DocumentType
        fields = '__all__'
        labels = {'name': 'Document Type Name'}


class DeliveryItemForm(forms.ModelForm):
    """Every visible cell of a delivery item row is rendered by the
    Tabulator grid in delivery_add.html/delivery_edit.html, not by this
    form -- these fields only exist so the grid's JS has somewhere to write
    the real model values into before a normal Django form POST. See
    `_delivery_items_table.html`."""

    class Meta:
        model = DeliveryItems
        fields = ['delivery_item', 'delivery_quantity', 'price_at_delivery', 'total_price_row', 'expiry_date']
        widgets = {
            'delivery_item': forms.HiddenInput(),
            'delivery_quantity': forms.HiddenInput(),
            'price_at_delivery': forms.HiddenInput(),
            'total_price_row': forms.HiddenInput(),
            'expiry_date': forms.HiddenInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['delivery_quantity'].required = True
        self.fields['delivery_item'].required = True


class BaseDeliveryItemFormSet(forms.BaseInlineFormSet):
    def clean(self):
        super().clean()

        has_valid_item = False

        for form in self.forms:
            if not hasattr(form, 'cleaned_data'):
                continue

            if form.cleaned_data.get('DELETE'):
                continue

            delivery_item = form.cleaned_data.get('delivery_item')
            delivery_quantity = form.cleaned_data.get('delivery_quantity')

            if delivery_item and delivery_quantity:
                has_valid_item = True
                break

        if not has_valid_item:
            raise forms.ValidationError('You must add at least one delivery item.')


DeliveryItemFormSet = inlineformset_factory(
    DeliveryAttributes,
    DeliveryItems,
    form=DeliveryItemForm,
    formset=BaseDeliveryItemFormSet,
    extra=1,
    can_delete=True,
)


