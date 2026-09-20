from django import forms
from django.core.validators import MaxLengthValidator
from django.forms import inlineformset_factory

from STORA.products.models import (
    Product, Category, Suppliers, Barcode, ProductSupplier, RecipeIngredient, TaxGroup,
)


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
            'internal_code', 'name', 'unit_type', 'delivery_price', 'sell_price', 'category', 'tax_group',
            'quantity', 'is_recipe', 'show_on_pos',
        ]

        labels = {
            'name': 'Product Name',
            'delivery_price': 'Last Delivery Price',
            'sell_price': 'Selling Price',
            'quantity': 'Current Stock',
            'show_on_pos': 'Show on POS screen',
        }

        widgets = {
            # A plain <select> doesn't scale as the category list grows
            # (subcategories can nest arbitrarily deep, see Category.parent)
            # -- product_create/edit.html render a search box instead (same
            # trigram-search pattern as the Supplier picker) and set this
            # hidden field's value via JS.
            'category': forms.HiddenInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 6 digits, not the model's full max_length=8 -- the shop's own
        # numbering scheme never needs more than that. Has to happen here,
        # not via Meta.widgets: CharField.__init__() re-derives `maxlength`
        # from the model field's max_length (8) and overwrites whatever
        # Meta.widgets set, at class-definition time -- confirmed live, the
        # attrs={'maxlength': 6} version above rendered as maxlength="8"
        # in the browser. Setting it here, after that's already happened,
        # is what actually sticks. max_length is set too so a value already
        # a bit longer than 6 (typed before JS/HTML5 could stop it, or
        # posted directly) still gets a clean validation error, not saved.
        code_field = self.fields['internal_code']
        code_field.widget.attrs.update({'inputmode': 'numeric', 'autocomplete': 'off'})
        # Only for a genuinely NEW product -- an existing one may already
        # carry a longer legacy code (model max_length is still 8), and
        # capping that retroactively here would block saving any OTHER
        # edit on that product too. Same "new only" guard as the tax_group
        # default above.
        if not self.instance.pk:
            code_field.max_length = 6
            code_field.validators = [
                v for v in code_field.validators if not isinstance(v, MaxLengthValidator)
            ]
            code_field.validators.append(MaxLengthValidator(6))
            code_field.widget.attrs['maxlength'] = 6
        # `disabled=True` (not just a readonly widget attr) makes Django
        # ignore whatever value is posted for this field and keep the
        # current one -- a readonly HTML attribute alone can be bypassed by
        # posting a different value directly.
        self.fields['quantity'].disabled = True
        self.fields['quantity'].help_text = (
            'Quantity cannot be changed manually. Use Deliveries, Sales, modules to update stock levels.'
        )
        # The model allows category=NULL (`null=True`, no `blank=True`) --
        # without this, Django's ModelForm still marks it required (it
        # derives `required` from `blank`, not `null`), which renders the
        # <select> with the HTML `required` attribute. That makes the
        # browser silently block the Save click with a native tooltip
        # whenever category is left unset -- no page change, no visible
        # error, easy to mistake for "did this even save?". Matches the
        # same fix already used in ProductInlineEditForm below.
        self.fields['category'].required = False

        # Every product in this shop is VAT group "Б" (20%) unless someone
        # picks something else -- defaulting the dropdown to it saves
        # re-selecting the same thing on nearly every new product. Only for
        # a genuinely NEW product (no pk yet) -- editing an existing one
        # must never silently override whatever tax_group it already has.
        # Looked up by name, not a hardcoded pk (which can differ between
        # dev/pilot/production databases) -- if "Б" doesn't exist in a
        # given database, this just quietly does nothing instead of
        # crashing the form.
        if not self.instance.pk:
            default_tax_group = TaxGroup.objects.filter(name='Б').first()
            if default_tax_group:
                self.fields['tax_group'].initial = default_tax_group.pk


class ProductInlineEditForm(forms.ModelForm):
    """Backs the Products grid's inline "Enable Edit" mode -- deliberately
    only the fields safe to edit cell-by-cell. `quantity` is excluded on
    purpose: it must only change via Deliveries/Sales, same rule as the
    regular edit form (see ProductForms)."""

    class Meta:
        model = Product
        fields = ['name', 'unit_type', 'category', 'sell_price', 'delivery_price', 'show_on_pos']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The model allows category=NULL (a product can be uncategorized);
        # match that here so the grid's "clear category" editor works.
        self.fields['category'].required = False


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ['name', 'description', 'parent', 'show_on_pos']

        labels = {
            'name': 'Category Name', 'description': 'Description', 'parent': 'Parent Category',
            'show_on_pos': 'Show on POS screen',
        }

        widgets = {
            # A plain <select> doesn't scale as the category tree grows --
            # category_form.html renders a search box + "Browse" tree modal
            # instead (same pattern as the Product form's own Category
            # field) and sets this hidden field's value via JS.
            'parent': forms.HiddenInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            # Excluding self + every descendant from the choices is enough
            # on its own to block a cycle -- an invalid value just fails
            # Django's normal ModelChoiceField "not a valid choice" check,
            # no separate clean_parent needed.
            excluded_ids = [self.instance.pk] + self.instance.get_descendant_ids()
            self.fields['parent'].queryset = Category.objects.exclude(pk__in=excluded_ids)


class TaxGroupForm(forms.ModelForm):
    class Meta:
        model = TaxGroup
        fields = '__all__'
        labels = {'name': 'Tax Group Name', 'rate': 'VAT Rate (%)'}


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
        fields = ['code', 'position', 'is_scale_code']
        labels = {
            'code': 'Barcode Number',
            'is_scale_code': 'Scale barcode (weight/qty encoded)',
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
            # A plain <select> doesn't scale (a shop can have many
            # suppliers) -- _supplier_formset.html renders a search box per
            # row instead (same trigram-search pattern as the Deliveries
            # supplier picker) and sets this hidden field's value via JS.
            'supplier': forms.HiddenInput(),
            # Same idea as Barcode.position -- kept in sync by JS numbering,
            # not typed in manually.
            'position': forms.HiddenInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['position'].required = False

    def has_changed(self):
        # `position` is renumbered by JS the moment a row exists (see
        # _supplier_formset.html), even for an "alternate supplier" row the
        # user never touched. Without this, Django's formset sees `position`
        # differ from its default and treats the whole (still-empty) row as
        # "changed" -- which defeats the normal extra-blank-form skip and
        # wrongly demands a `supplier` value for a row that was never meant
        # to be filled in. Only `supplier` itself should decide that.
        return 'supplier' in self.changed_data


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
        widgets = {
            # Rendering the full product queryset as <option>s doesn't scale
            # (a shop can have thousands of products) -- the picker in
            # _recipe_formset.html sets this value via JS after the user
            # searches/picks a product through IngredientSearchView instead.
            # ModelChoiceField still validates the submitted pk against the
            # queryset below regardless of widget, so this stays just as safe.
            'ingredient': forms.HiddenInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A recipe can't contain another recipe (no nested recipes) -- kept
        # as the validation queryset even though the widget no longer lists
        # options from it directly.
        self.fields['ingredient'].queryset = Product.objects.filter(is_recipe=False).order_by('name')


RecipeIngredientFormSet = inlineformset_factory(
    Product,
    RecipeIngredient,
    form=RecipeIngredientForm,
    fk_name='recipe',
    # extra=0, not 1 -- rows are only ever added by picking a search result
    # in _recipe_formset.html's JS, never by showing a blank unfilled row.
    extra=0,
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