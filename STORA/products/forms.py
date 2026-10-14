from django import forms
from django.core.validators import MaxLengthValidator
from django.forms import inlineformset_factory
from django.utils.translation import gettext_lazy as _

from STORA.products.models import (
    Product, Category, Suppliers, Barcode, ProductSupplier, RecipeIngredient, TaxGroup, ProductAttribute,
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
            'name': _('Product Name'),
            'delivery_price': _('Last Delivery Price'),
            'sell_price': _('Selling Price'),
            'quantity': _('Current Stock'),
            'show_on_pos': _('Show on POS screen'),
        }

        # Replaces Django's own default uniqueness message ("Product with
        # this product name already exists.") -- grammatically awkward
        # once translated to Bulgarian (field's verbose_name inserted raw
        # mid-sentence: "Продукт с този Име на продукта вече съществува.").
        # 'unique' is the exact error code Model.validate_unique() raises
        # for a unique=True field (see BaseModelForm._update_errors,
        # matched by ValidationError.code) -- Django substitutes this
        # message in automatically, nothing else needs to change.
        error_messages = {
            'name': {'unique': _('A product with this name already exists.')},
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
        self.fields['quantity'].help_text = _(
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

        # The field itself is a `.field--tiny` narrow column (see
        # product_create.html) -- the model's own choices ("Piece (pcs)"/
        # "Weight (kg)") were overflowing that width, so this form
        # specifically shows just "pcs"/"kg" instead. Only overrides what
        # this ONE form's <select> renders, not Product.UNIT_TYPE_CHOICES
        # itself -- get_unit_type_display() elsewhere (product detail page,
        # history log, ...) keeps the fuller wording.
        self.fields['unit_type'].choices = [(Product.PIECE, _('pcs')), (Product.WEIGHT, _('kg'))]

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

        # One extra field per shop-defined ProductAttribute (Color, Size,
        # whatever this shop decided to track -- see that model's own
        # docstring for why this is a flat global list, not scoped to
        # category). These are NOT real Product model fields -- Meta.fields
        # above only lists real columns -- so they're added here by hand
        # and collected back into Product.attributes in save() below,
        # same general idea as how `supplier` is kept out of Meta.fields
        # and handled by its own separate formset instead. A dropdown for
        # a predefined choices list, plain text otherwise -- empty/unset
        # is always allowed (required=False) regardless, since most
        # products won't have every attribute this shop happens to track.
        self.attribute_fields = []
        for attribute in ProductAttribute.objects.all():
            field_name = f'attribute_{attribute.pk}'
            self.attribute_fields.append((attribute.pk, field_name))
            initial = self.instance.attributes.get(str(attribute.pk), '')
            if attribute.choices:
                self.fields[field_name] = forms.ChoiceField(
                    choices=[('', '---------')] + [(c, c) for c in attribute.choices],
                    required=False, label=attribute.name, initial=initial,
                )
            else:
                self.fields[field_name] = forms.CharField(
                    required=False, label=attribute.name, initial=initial,
                )

    def save(self, commit=True):
        instance = super().save(commit=False)
        instance.attributes = {
            str(attribute_id): self.cleaned_data[field_name]
            for attribute_id, field_name in self.attribute_fields
            if self.cleaned_data.get(field_name)
        }
        if commit:
            instance.save()
            self._save_m2m()
        return instance

    @property
    def attribute_bound_fields(self):
        # product_create/edit.html can't reference attribute_<id> fields by
        # name directly (the id isn't known until render time) -- this
        # hands the template ready-to-render BoundFields in the same order
        # attribute_fields was built, so it can just loop over them.
        return [self[field_name] for _, field_name in self.attribute_fields]


class ProductInlineEditForm(forms.ModelForm):
    """Backs the Products grid's inline "Enable Edit" mode -- deliberately
    only the fields safe to edit cell-by-cell. `quantity` is excluded on
    purpose: it must only change via Deliveries/Sales, same rule as the
    regular edit form (see ProductForms)."""

    class Meta:
        model = Product
        fields = ['name', 'unit_type', 'category', 'sell_price', 'delivery_price', 'show_on_pos', 'is_archived']

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
            'name': _('Category Name'), 'description': _('Description'), 'parent': _('Parent Category'),
            'show_on_pos': _('Show on POS screen'),
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


class ProductAttributeForm(forms.ModelForm):
    # Overrides the model's own JSONField(choices) -- its default form
    # widget would render/accept raw JSON (`["S", "M", "L"]`), not
    # something a Manager should have to type correctly by hand. One value
    # per line instead, converted to/from the stored list below.
    choices = forms.CharField(
        required=False, widget=forms.Textarea(attrs={'rows': 4}),
        label=_('Choices'),
        help_text=_('One value per line. Leave empty for a free-text field on the product form.'),
    )

    class Meta:
        model = ProductAttribute
        fields = ['name', 'choices']
        labels = {'name': _('Attribute Name')}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            # self.initial (form-level), not self.fields['choices'].initial
            # (field-level) -- ModelForm.__init__ already populated
            # self.initial['choices'] from the raw model value (a list,
            # via model_to_dict), and BoundField.value() checks form.initial
            # BEFORE field.initial, so only overwriting the field-level one
            # silently did nothing (confirmed live: the edit form rendered
            # the raw Python list repr instead of one choice per line).
            self.initial['choices'] = '\n'.join(self.instance.choices)

    def clean_choices(self):
        raw = self.cleaned_data['choices']
        return [line.strip() for line in raw.splitlines() if line.strip()]


class TaxGroupForm(forms.ModelForm):
    class Meta:
        model = TaxGroup
        fields = '__all__'
        labels = {'name': _('Tax Group Name'), 'rate': _('VAT Rate (%)'), 'fiscal_letter': _('Fiscal device letter')}
        help_texts = {
            'fiscal_letter': _(
                'The single Cyrillic letter (e.g. Б, Г) this rate is programmed as on the fiscal '
                'device -- leave blank if this group is never sold through the fiscal printer.'
            ),
        }


class SuppliersForm(forms.ModelForm):
    class Meta:
        model = Suppliers
        fields = '__all__'

        labels = {
            'name': _('Supplier Name'),
            'bulstat': _('BULSTAT'),
            'vat_n': _('VAT Number'),
            'phone': _('Phone Number'),
            'email': _('Email Address'),
        }


class BarcodeForm(forms.ModelForm):
    class Meta:
        model = Barcode
        fields = ['code', 'position', 'is_scale_code']
        labels = {
            'code': _('Barcode Number'),
            'is_scale_code': _('Scale barcode (weight/qty encoded)'),
        }
        widgets = {
            'code': forms.TextInput(attrs={'placeholder': _('Scan or enter barcode')}),
            # The "Barcode #1/#2/#3" numbering in the UI IS the position --
            # the JS renumber() in _barcode_formset.html keeps this in sync,
            # no manual input needed.
            'position': forms.HiddenInput(),
        }

    def has_changed(self):
        # renumber() (see _barcode_formset.html) keeps every barcode row's
        # hidden `position` field in sync with its spot in the DOM,
        # including the always-present blank "extra" row (formset extra=1
        # -- a brand-new product starts with one ready-to-fill barcode
        # field) whenever there's more than one row on the page. That
        # renumbering alone makes `position` differ from its default
        # (model default is 1; a 2nd/3rd row gets renumbered to 2/3), so
        # Django's formset sees this still-empty row as "changed" and
        # saves it as a genuine Barcode(code=None) row on every single
        # product save -- confirmed live (an edited product quietly grew
        # an extra blank barcode row each time it was saved). Only
        # `position` differing, on a row that was never actually saved
        # before, doesn't count as a real change. An EXISTING barcode
        # (already has a pk) reporting only `position` changed is a
        # legitimate reorder (e.g. an earlier row got deleted) and must
        # still save normally -- this only short-circuits brand-new,
        # still-blank rows.
        changed = set(self.changed_data)
        if not self.instance.pk and changed and changed <= {'position'}:
            return False
        return bool(changed)

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
        labels = {'quantity': _('Quantity per unit (kg for weight, pcs for piece)')}
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
        label=_('From date'),
        widget=forms.DateInput(attrs={'type': 'date'}),
    )
    end_date = forms.DateField(
        label=_('To date'),
        widget=forms.DateInput(attrs={'type': 'date'}),
    )

    def clean(self):
        cleaned_data = super().clean()
        start_date = cleaned_data.get('start_date')
        end_date = cleaned_data.get('end_date')

        if start_date and end_date and start_date > end_date:
            raise forms.ValidationError(_('Start date cannot be later than end date.'))

        return cleaned_data