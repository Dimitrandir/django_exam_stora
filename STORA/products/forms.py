from django import forms
from django.forms import inlineformset_factory

from STORA.products.models import Product, Category, Suppliers, Barcode, ProductSupplier, RecipeIngredient


class ProductForms(forms.ModelForm):
    class Meta:
        model = Product
        # Not '__all__' -- `supplier` is deliberately left out. Django does
        # NOT auto-exclude a through= M2M from a ModelForm; it renders as a
        # normal (and here, unused/empty) field. Since nothing ever submits
        # a value for it, save() would call `instance.supplier.set([])` and
        # silently wipe out whatever the supplier formset just saved.
        # Suppliers are managed entirely through ProductSupplierFormSet.
        fields = [
            'internal_code', 'name', 'unit_type', 'delivery_price', 'sell_price', 'category', 'quantity',
            'is_recipe',
        ]

        labels = {
            'name': 'Product Name',
            'delivery_price': 'Last Delivery Price',
            'sell_price': 'Selling Price',
            'quantity': 'Current Stock',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # `disabled=True` (not just a readonly widget attr) makes Django
        # ignore whatever value is posted for this field and keep the
        # current one -- a readonly HTML attribute alone can be bypassed by
        # posting a different value directly.
        self.fields['quantity'].disabled = True
        self.fields['quantity'].help_text = (
            'Quantity cannot be changed manually. Use Deliveries, Sales, modules to update stock levels.'
        )


class ProductInlineEditForm(forms.ModelForm):
    """Backs the Products grid's inline "Enable Edit" mode -- deliberately
    only the 4 fields safe to edit cell-by-cell. `quantity` is excluded on
    purpose: it must only change via Deliveries/Sales, same rule as the
    regular edit form (see ProductForms)."""

    class Meta:
        model = Product
        fields = ['name', 'unit_type', 'category', 'sell_price', 'delivery_price']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The model allows category=NULL (a product can be uncategorized);
        # match that here so the grid's "clear category" editor works.
        self.fields['category'].required = False


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = '__all__'

        labels = {'name': 'Category Name', 'description': 'Description'}


class SuppliersForm(forms.ModelForm):
    class Meta:
        model = Suppliers
        fields = '__all__'

        labels = {
            'name': 'Supplier Name',
            'bulstat': 'BULSTAT',
            'vat_n': 'VAT Number',
            'phone': 'Phone Number',
            'email': 'Email Address',
        }


class BarcodeForm(forms.ModelForm):
    class Meta:
        model = Barcode
        fields = ['code', 'position']
        labels = {
            'code': 'Barcode Number',
        }
        widgets = {
            'code': forms.TextInput(attrs={'placeholder': 'Scan or enter barcode'}),
            # The "Barcode #1/#2/#3" numbering in the UI IS the position --
            # the JS renumber() in _barcode_formset.html keeps this in sync,
            # no manual input needed.
            'position': forms.HiddenInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Falls back to the model's default (1) if the hidden field is
        # somehow missing from the POST, instead of a hard validation error.
        self.fields['position'].required = False


BarcodeFormSet = inlineformset_factory(
    Product,
    Barcode,
    form=BarcodeForm,
    extra=1,
    can_delete=True,
)


class ProductSupplierForm(forms.ModelForm):
    class Meta:
        model = ProductSupplier
        fields = ['supplier', 'position']
        widgets = {
            # Same idea as Barcode.position -- kept in sync by JS numbering,
            # not typed in manually.
            'position': forms.HiddenInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['position'].required = False


ProductSupplierFormSet = inlineformset_factory(
    Product,
    ProductSupplier,
    form=ProductSupplierForm,
    fk_name='product',
    extra=1,
    can_delete=True,
)


class RecipeIngredientForm(forms.ModelForm):
    class Meta:
        model = RecipeIngredient
        fields = ['ingredient', 'quantity']
        labels = {'quantity': 'Quantity per unit (kg for weight, pcs for piece)'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A recipe can't contain another recipe (no nested recipes) -- keep
        # that impossible to pick in the UI, not just a validation error
        # after the fact.
        self.fields['ingredient'].queryset = Product.objects.filter(is_recipe=False).order_by('name')


RecipeIngredientFormSet = inlineformset_factory(
    Product,
    RecipeIngredient,
    form=RecipeIngredientForm,
    fk_name='recipe',
    extra=1,
    can_delete=True,
)


class ProductHistoryPeriodForm(forms.Form):
    start_date = forms.DateField(
        label='From date',
        widget=forms.DateInput(attrs={'type': 'date'}),
    )
    end_date = forms.DateField(
        label='To date',
        widget=forms.DateInput(attrs={'type': 'date'}),
    )

    def clean(self):
        cleaned_data = super().clean()
        start_date = cleaned_data.get('start_date')
        end_date = cleaned_data.get('end_date')

        if start_date and end_date and start_date > end_date:
            raise forms.ValidationError('Start date cannot be later than end date.')

        return cleaned_data