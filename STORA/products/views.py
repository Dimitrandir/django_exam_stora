from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.postgres.search import TrigramSimilarity
from django.db.models import ProtectedError, Sum
from django.forms.models import model_to_dict
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.http import urlencode
from django.views import View
from django.views.generic import CreateView, ListView, DetailView, UpdateView, DeleteView

from STORA.core.mixins import StaffPermissionRequiredMixin
from STORA.core.session_service import get_cashier_operation_state
from STORA.core.utils import dispatch_task, multi_token_icontains_q
from STORA.deliveries.models import DeliveryItems
from STORA.products.forms import (
    ProductForms, ProductInlineEditForm, CategoryForm, SuppliersForm, BarcodeFormSet, ProductSupplierFormSet,
    RecipeIngredientFormSet, ProductHistoryPeriodForm, TaxGroupForm,
)
from STORA.products.models import Product, Barcode, Category, Suppliers, TaxGroup
from STORA.sales.models import SaleAttributes, SaleItems
from STORA.sales.tasks import backfill_recipe_ingredient_stock


def get_free_internal_codes():
    """(first free, next free) internal_code numbers -- only considers
    codes that are purely digits (internal_code is a free-text CharField,
    so non-numeric codes are just ignored for this suggestion).
    first free = smallest unused number counting up from 1 (fills gaps).
    next free = one past the highest number in use (appends at the end)."""
    used = {
        int(code) for code in Product.objects.values_list('internal_code', flat=True)
        if code and code.isdigit()
    }
    first_free = 1
    while first_free in used:
        first_free += 1
    next_free = max(used) + 1 if used else 1
    return first_free, next_free


class ProductListView(LoginRequiredMixin, StaffPermissionRequiredMixin, ListView):
    permission_required = 'products.view_product'
    model = Product
    template_name = 'products/products_list.html'
    context_object_name = 'products'

    #: how many barcode/supplier columns the grid shows -- a product can
    #: have more, but the grid needs a fixed number of columns; anything
    #: past this is still visible on the product's own detail page.
    GRID_SLOT_COUNT = 3

    def get_queryset(self):
        return Product.objects.select_related('category', 'tax_group').prefetch_related(
            'barcode', 'product_suppliers__supplier'
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        products_data = []
        for product in context['products']:
            barcodes = list(product.barcode.all())
            suppliers = list(product.product_suppliers.all())
            # Computed server-side (like everything else in this row) rather
            # than as a Tabulator mutator -- a mutator would also re-run on
            # every edit COMMIT for this column, silently overwriting
            # whatever markup % the user just typed with the recomputed-
            # from-current-prices value before the edit could ever be saved.
            markup_percent = None
            if product.delivery_price:
                markup_percent = round(
                    (float(product.sell_price or 0) - float(product.delivery_price)) / float(product.delivery_price) * 100,
                    1,
                )
            row = {
                'id': product.pk,
                'internal_code': product.internal_code,
                'name': product.name,
                'category': product.category.name if product.category else '',
                'tax_group': product.tax_group.name if product.tax_group else '',
                'unit_type': product.unit_type,
                'is_recipe': product.is_recipe,
                'show_on_pos': product.show_on_pos,
                'quantity': float(product.quantity),
                'sell_price': float(product.sell_price) if product.sell_price is not None else None,
                'delivery_price': float(product.delivery_price) if product.delivery_price is not None else None,
                'markup_percent': markup_percent,
                'view_url': reverse('product_details', kwargs={'pk': product.pk}),
            }
            for i in range(self.GRID_SLOT_COUNT):
                row[f'barcode_{i + 1}'] = barcodes[i].code if i < len(barcodes) else ''
                row[f'supplier_{i + 1}'] = suppliers[i].supplier.name if i < len(suppliers) else ''
            products_data.append(row)

        context['products_data'] = products_data
        context['category_choices'] = list(Category.objects.order_by('name').values_list('name', flat=True))
        return context


class ProductInlineUpdateView(LoginRequiredMixin, StaffPermissionRequiredMixin, View):
    """Saves one cell edited in the Products grid's "Enable Edit" mode.
    Reuses ProductInlineEditForm so the same validation as the regular Edit
    page applies (unique name, positive price, etc.)."""

    permission_required = 'products.change_product'

    def post(self, request, pk):
        product = get_object_or_404(Product, pk=pk)
        field = request.POST.get('field')
        value = request.POST.get('value', '')

        if field not in ProductInlineEditForm.base_fields:
            return JsonResponse({'error': 'That field cannot be edited here.'}, status=400)

        data = model_to_dict(product, fields=list(ProductInlineEditForm.base_fields))

        if field == 'category':
            category = Category.objects.filter(name=value).first() if value else None
            if value and not category:
                return JsonResponse({'error': 'Unknown category.'}, status=400)
            data['category'] = category.pk if category else ''
        else:
            data[field] = value

        form = ProductInlineEditForm(data=data, instance=product)
        if not form.is_valid():
            first_error = next(iter(form.errors.values()))[0]
            return JsonResponse({'error': first_error}, status=400)

        # See ProductUpdateView -- lets the ProductChangeLog signal
        # attribute this change to whoever made it.
        form.instance._changed_by = request.user
        form.save()
        return JsonResponse({
            'name': product.name,
            'category': product.category.name if product.category else '',
            'sell_price': float(product.sell_price) if product.sell_price is not None else None,
            'delivery_price': float(product.delivery_price) if product.delivery_price is not None else None,
        })


class ProductBulkActionView(LoginRequiredMixin, View):
    """Applies one action to a batch of products selected via the checkbox
    column in the Products grid. Each action checks its own permission
    (change vs. delete) since a single static `permission_required` can't
    express "change_product for these two, delete_product for that one")."""

    def post(self, request):
        action = request.POST.get('action')
        ids = request.POST.getlist('ids')

        if not ids:
            return JsonResponse({'error': 'No products selected.'}, status=400)

        products = Product.objects.filter(pk__in=ids)

        if action == 'set_category':
            if not request.user.has_perm('products.change_product'):
                return JsonResponse({'error': 'You do not have permission to do this.'}, status=403)
            value = request.POST.get('value', '')
            category = None
            if value:
                category = Category.objects.filter(name=value).first()
                if not category:
                    return JsonResponse({'error': 'Unknown category.'}, status=400)
            updated = products.update(category=category)
            return JsonResponse({'updated': updated})

        if action == 'set_sell_price':
            if not request.user.has_perm('products.change_product'):
                return JsonResponse({'error': 'You do not have permission to do this.'}, status=403)
            value = request.POST.get('value', '')
            try:
                price = Decimal(value)
            except (InvalidOperation, ValueError):
                return JsonResponse({'error': 'Enter a valid price.'}, status=400)
            if price < Decimal('0.01'):
                return JsonResponse({'error': 'Price must be at least 0.01.'}, status=400)
            updated = products.update(sell_price=price)
            return JsonResponse({'updated': updated})

        if action == 'delete':
            if not request.user.has_perm('products.delete_product'):
                return JsonResponse({'error': 'You do not have permission to do this.'}, status=403)
            # Delete one at a time -- a single protected product in a bulk
            # QuerySet.delete() would abort the whole batch and leave
            # nothing deleted, even the unprotected ones.
            deleted, blocked = 0, []
            for product in products:
                try:
                    product.delete()
                    deleted += 1
                except ProtectedError:
                    blocked.append(product.name)
            response = {'deleted': deleted}
            if blocked:
                response['error'] = (
                    'Could not delete (has sale/delivery history): ' + ', '.join(blocked)
                )
            return JsonResponse(response, status=200 if deleted else 400)

        return JsonResponse({'error': 'Unknown action.'}, status=400)


class ProductDetailView(LoginRequiredMixin, StaffPermissionRequiredMixin, DetailView):
    permission_required = 'products.view_product'
    model = Product
    template_name = 'products/products_details.html'
    context_object_name = 'product'

    RECENT_EVENTS_LIMIT = 5

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        product = self.object

        if product.tax_group:
            divisor = Decimal('1') + product.tax_group.rate / Decimal('100')
            if product.delivery_price is not None:
                context['delivery_price_without_vat'] = (product.delivery_price / divisor).quantize(Decimal('0.01'))
            if product.sell_price is not None:
                context['sell_price_without_vat'] = (product.sell_price / divisor).quantize(Decimal('0.01'))

        # Falsy check on purpose -- also skips a zero delivery_price (never
        # entered yet), which would otherwise divide by zero.
        if product.delivery_price and product.sell_price is not None:
            context['markup_percent'] = (
                (product.sell_price - product.delivery_price) / product.delivery_price * Decimal('100')
            ).quantize(Decimal('0.1'))

        recent_sales = [
            {'date': item.sale.time_of_sale, 'event_type': 'Sale', 'quantity_change': -item.sale_quantity}
            for item in SaleItems.objects.filter(sale_item=product)
                .select_related('sale').order_by('-sale__time_of_sale')[:self.RECENT_EVENTS_LIMIT]
        ]
        recent_deliveries = [
            {'date': item.delivery.time_of_delivery, 'event_type': 'Delivery', 'quantity_change': item.delivery_quantity}
            for item in DeliveryItems.objects.filter(delivery_item=product)
                .select_related('delivery').order_by('-delivery__time_of_delivery')[:self.RECENT_EVENTS_LIMIT]
        ]
        context['recent_events'] = sorted(
            recent_sales + recent_deliveries, key=lambda event: event['date'], reverse=True
        )[:self.RECENT_EVENTS_LIMIT]
        return context


class ProductHistoryView(LoginRequiredMixin, StaffPermissionRequiredMixin, DetailView):
    """Timeline of everything that moved this product's stock (sales and
    deliveries) within a chosen date range, newest first."""

    permission_required = 'products.view_product'
    model = Product
    template_name = 'products/product_history.html'
    context_object_name = 'product'

    def get_default_period(self):
        end_date = timezone.localdate()
        start_date = end_date - timedelta(days=30)
        return start_date, end_date

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        product = self.object

        default_start, default_end = self.get_default_period()
        form = ProductHistoryPeriodForm(
            self.request.GET or None,
            initial={'start_date': default_start, 'end_date': default_end},
        )

        if form.is_valid():
            start_date = form.cleaned_data['start_date']
            end_date = form.cleaned_data['end_date']
        else:
            start_date, end_date = default_start, default_end

        sales = SaleItems.objects.filter(
            sale_item=product,
            sale__time_of_sale__date__range=(start_date, end_date),
        ).select_related('sale', 'sale__cashier')

        deliveries = DeliveryItems.objects.filter(
            delivery_item=product,
            delivery__time_of_delivery__date__range=(start_date, end_date),
        ).select_related('delivery', 'delivery__supplier')

        events = [
            {
                'date': item.sale.time_of_sale,
                'event_type': 'Sale',
                'quantity_change': -item.sale_quantity,
                'unit_price': item.price_at_sale,
                'supplier': None,
                'cashier': item.sale.cashier,
            }
            for item in sales
        ] + [
            {
                'date': item.delivery.time_of_delivery,
                'event_type': 'Delivery',
                'quantity_change': item.delivery_quantity,
                'unit_price': item.price_at_delivery,
                'supplier': item.delivery.supplier,
                'cashier': None,
            }
            for item in deliveries
        ]
        events.sort(key=lambda event: event['date'], reverse=True)

        context['form'] = form
        context['start_date'] = start_date
        context['end_date'] = end_date
        context['events'] = events
        context['events_data'] = [
            {
                'date': event['date'].strftime('%Y-%m-%d %H:%M'),
                'event_type': event['event_type'],
                'quantity_change': float(event['quantity_change']),
                'unit_price': float(event['unit_price']) if event['unit_price'] is not None else None,
                'supplier': str(event['supplier']) if event['supplier'] else '',
                'cashier': str(event['cashier']) if event['cashier'] else '',
            }
            for event in events
        ]

        changes = product.change_log.filter(
            changed_at__date__range=(start_date, end_date)
        ).select_related('changed_by')
        context['changes_data'] = [
            {
                'date': change.changed_at.strftime('%Y-%m-%d %H:%M'),
                'field_name': change.field_name,
                'old_value': change.old_value,
                'new_value': change.new_value,
                'changed_by': str(change.changed_by) if change.changed_by else '',
            }
            for change in changes
        ]
        return context


class IngredientSearchView(LoginRequiredMixin, StaffPermissionRequiredMixin, View):
    """Backs the recipe-ingredient picker on the product create/edit pages
    (see `_recipe_formset.html`). A plain `<select>` with the full product
    catalog as `<option>`s doesn't scale once there are thousands of
    products, so instead of rendering that queryset into the page, the JS
    calls this endpoint on every keystroke (debounced) and only ever gets
    back a handful of matches -- same trigram-search approach as the navbar
    search (`GlobalSearchView`), scoped down to candidate ingredients."""

    permission_required = 'products.view_product'
    RESULTS_LIMIT = 30

    def get(self, request):
        query = request.GET.get('q', '').strip()
        category_id = request.GET.get('category', '').strip()

        # A recipe can't contain another recipe -- same rule as
        # RecipeIngredientForm's queryset.
        products = Product.objects.filter(is_recipe=False)
        if category_id:
            products = products.filter(category_id=category_id)

        if query:
            products = (
                products
                .filter(multi_token_icontains_q(query, ['name', 'internal_code']))
                .annotate(similarity=TrigramSimilarity('name', query))
                .order_by('-similarity', 'name')
            )
        else:
            products = products.order_by('name')

        products = products.select_related('category')[:self.RESULTS_LIMIT]

        return JsonResponse({'results': [
            {
                'id': product.pk,
                'name': product.name,
                'internal_code': product.internal_code,
                'category': product.category.name if product.category else '',
                'unit_type': product.unit_type,
                'delivery_price': float(product.delivery_price) if product.delivery_price is not None else 0,
            }
            for product in products
        ]})


class SupplierSearchView(LoginRequiredMixin, StaffPermissionRequiredMixin, View):
    """Backs the searchable supplier picker on the delivery create/edit
    pages -- same reasoning as IngredientSearchView: a plain `<select>` with
    every supplier as an `<option>` doesn't scale, so the JS queries this
    endpoint instead of rendering the full queryset into the page."""

    permission_required = 'products.view_suppliers'
    RESULTS_LIMIT = 30

    def get(self, request):
        query = request.GET.get('q', '').strip()
        suppliers = Suppliers.objects.all()

        if query:
            suppliers = (
                suppliers
                .filter(multi_token_icontains_q(query, ['name', 'bulstat']))
                .annotate(similarity=TrigramSimilarity('name', query))
                .order_by('-similarity', 'name')
            )
        else:
            suppliers = suppliers.order_by('name')

        suppliers = suppliers[:self.RESULTS_LIMIT]

        return JsonResponse({'results': [
            {'id': supplier.pk, 'name': supplier.name, 'bulstat': supplier.bulstat}
            for supplier in suppliers
        ]})


class ProductCreateView(LoginRequiredMixin, StaffPermissionRequiredMixin, CreateView):
    permission_required = 'products.add_product'
    model = Product
    form_class = ProductForms
    template_name = 'products/product_create.html'
    success_url = reverse_lazy('product_list')

    def get_initial(self):
        # "Save and New" (see form_valid) redirects back here with these in
        # the query string, so the next product in the same batch doesn't
        # need them re-picked by hand.
        initial = super().get_initial()
        if 'category' in self.request.GET:
            initial['category'] = self.request.GET['category']
        if 'tax_group' in self.request.GET:
            initial['tax_group'] = self.request.GET['tax_group']
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.POST:
            context['barcode_formset'] = BarcodeFormSet(self.request.POST)
            context['supplier_formset'] = ProductSupplierFormSet(self.request.POST, prefix='supplier')
            context['recipe_formset'] = RecipeIngredientFormSet(self.request.POST, prefix='recipe')
        else:
            context['barcode_formset'] = BarcodeFormSet()
            context['supplier_formset'] = ProductSupplierFormSet(prefix='supplier')
            context['recipe_formset'] = RecipeIngredientFormSet(prefix='recipe')
        context['first_free_code'], context['next_free_code'] = get_free_internal_codes()
        context['tax_group_rates'] = {str(tg.pk): str(tg.rate) for tg in TaxGroup.objects.all()}
        return context

    def form_valid(self, form):
        context = self.get_context_data()
        barcode_formset = context['barcode_formset']
        supplier_formset = context['supplier_formset']
        recipe_formset = context['recipe_formset']

        if not barcode_formset.is_valid() or not supplier_formset.is_valid() or not recipe_formset.is_valid():
            return self.render_to_response(context)

        self.object = form.save()
        barcode_formset.instance = self.object
        barcode_formset.save()
        supplier_formset.instance = self.object
        supplier_formset.save()
        recipe_formset.instance = self.object
        recipe_formset.save()
        self.object.recalculate_ingredient_cost()
        # A brand new product can't have prior sales, so there's nothing to
        # backfill -- but harmless/idempotent to call regardless (matches
        # ProductUpdateView, keeps the "just defined a recipe" path in one
        # place conceptually).
        if self.object.is_recipe and self.object.ingredients_backfilled_at is None:
            dispatch_task(backfill_recipe_ingredient_stock, self.object.pk)

        if 'save_and_new' in self.request.POST:
            # Category/Tax Group carry over via the query string (read back
            # in get_initial) -- handy when adding a batch of products from
            # the same delivery/category one after another.
            params = {}
            if self.object.category_id:
                params['category'] = self.object.category_id
            if self.object.tax_group_id:
                params['tax_group'] = self.object.tax_group_id
            url = reverse('product_create')
            if params:
                url += '?' + urlencode(params)
            return HttpResponseRedirect(url)

        return super().form_valid(form)


class ProductUpdateView(LoginRequiredMixin, StaffPermissionRequiredMixin, UpdateView):
    permission_required = 'products.change_product'
    model = Product
    form_class = ProductForms
    template_name = 'products/product_edit.html'
    context_object_name = 'product'
    success_url = reverse_lazy('product_list')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.POST:
            context['barcode_formset'] = BarcodeFormSet(self.request.POST, instance=self.object)
            context['supplier_formset'] = ProductSupplierFormSet(
                self.request.POST, instance=self.object, prefix='supplier'
            )
            context['recipe_formset'] = RecipeIngredientFormSet(
                self.request.POST, instance=self.object, prefix='recipe'
            )
        else:
            context['barcode_formset'] = BarcodeFormSet(instance=self.object)
            context['supplier_formset'] = ProductSupplierFormSet(instance=self.object, prefix='supplier')
            context['recipe_formset'] = RecipeIngredientFormSet(instance=self.object, prefix='recipe')
        context['first_free_code'], context['next_free_code'] = get_free_internal_codes()
        context['tax_group_rates'] = {str(tg.pk): str(tg.rate) for tg in TaxGroup.objects.all()}
        return context

    def form_valid(self, form):
        context = self.get_context_data()
        barcode_formset = context['barcode_formset']
        supplier_formset = context['supplier_formset']
        recipe_formset = context['recipe_formset']

        if not barcode_formset.is_valid() or not supplier_formset.is_valid() or not recipe_formset.is_valid():
            return self.render_to_response(context)

        # Read by the post_save signal in models.py (ProductChangeLog) so
        # each logged field change is attributed to whoever made it -- the
        # signal itself has no access to the request/user.
        form.instance._changed_by = self.request.user

        self.object = form.save()
        barcode_formset.instance = self.object
        barcode_formset.save()
        supplier_formset.instance = self.object
        supplier_formset.save()
        recipe_formset.instance = self.object
        recipe_formset.save()
        self.object.recalculate_ingredient_cost()

        # First time this product becomes a recipe (or was already one but
        # never backfilled): catch up ingredient stock for all its past
        # sales, in the background so saving the form doesn't wait on it.
        if self.object.is_recipe and self.object.ingredients_backfilled_at is None:
            dispatch_task(backfill_recipe_ingredient_stock, self.object.pk)

        return super().form_valid(form)


class ProductDeleteView(LoginRequiredMixin, StaffPermissionRequiredMixin, DeleteView):
    permission_required = 'products.delete_product'
    model = Product
    template_name = 'products/product_confirm_delete.html'
    success_url = reverse_lazy('product_list')

    def form_valid(self, form):
        try:
            self.object.delete()
        except ProtectedError:
            context = self.get_context_data(
                protected_error=(
                    "Cannot delete this product -- it has delivery or sale "
                    "history linked to it."
                )
            )
            return self.render_to_response(context)
        return HttpResponseRedirect(self.get_success_url())


class CategoryListView(LoginRequiredMixin, StaffPermissionRequiredMixin, ListView):
    permission_required = 'products.view_category'
    model = Category
    template_name = 'products/category_list.html'
    context_object_name = 'categories'

    def get_queryset(self):
        return super().get_queryset().select_related('parent')


class CategoryTogglePosView(LoginRequiredMixin, StaffPermissionRequiredMixin, View):
    """Backs the single checkbox column on the Categories list -- the list
    itself is still a plain HTML table (see CLAUDE.md, Tabulator-ifying it
    is a separate Фаза 1.5 item), so this is a lightweight one-field toggle
    rather than the full Products-grid inline-edit machinery."""

    permission_required = 'products.change_category'

    def post(self, request, pk):
        category = get_object_or_404(Category, pk=pk)
        category.show_on_pos = request.POST.get('value') == 'true'
        category.save(update_fields=['show_on_pos'])
        return JsonResponse({'show_on_pos': category.show_on_pos})


class CategoryCreateView(LoginRequiredMixin, StaffPermissionRequiredMixin, CreateView):
    permission_required = 'products.add_category'
    model = Category
    form_class = CategoryForm
    template_name = 'products/category_form.html'
    success_url = reverse_lazy('category_list')

    def form_valid(self, form):
        response = super().form_valid(form)
        # "+ New category" on the product form opens this in a popup window
        # (?popup=1) so the in-progress product form isn't lost -- closing
        # the popup instead of redirecting it to the categories list is what
        # lets the opener pick up the new category without a full reload.
        if self.request.GET.get('popup'):
            return render(self.request, 'products/_popup_close.html', {
                'created_id': self.object.pk,
                'created_name': self.object.name,
                'message_type': 'category-created',
            })
        return response


class CategoryUpdateView(LoginRequiredMixin, StaffPermissionRequiredMixin, UpdateView):
    permission_required = 'products.change_category'
    model = Category
    form_class = CategoryForm
    template_name = 'products/category_form.html'
    context_object_name = 'category'
    success_url = reverse_lazy('category_list')


class CategoryDeleteView(LoginRequiredMixin, StaffPermissionRequiredMixin, DeleteView):
    permission_required = 'products.delete_category'
    model = Category
    template_name = 'products/category_confirm_delete.html'
    success_url = reverse_lazy('category_list')


class TaxGroupListView(LoginRequiredMixin, StaffPermissionRequiredMixin, ListView):
    permission_required = 'products.view_taxgroup'
    model = TaxGroup
    template_name = 'products/tax_group_list.html'
    context_object_name = 'tax_groups'


class TaxGroupCreateView(LoginRequiredMixin, StaffPermissionRequiredMixin, CreateView):
    permission_required = 'products.add_taxgroup'
    model = TaxGroup
    form_class = TaxGroupForm
    template_name = 'products/tax_group_form.html'
    success_url = reverse_lazy('tax_group_list')

    def form_valid(self, form):
        response = super().form_valid(form)
        # Same popup pattern as CategoryCreateView/SupplierCreateView.
        if self.request.GET.get('popup'):
            return render(self.request, 'products/_popup_close.html', {
                'created_id': self.object.pk,
                'created_name': str(self.object),
                'created_rate': self.object.rate,
                'message_type': 'tax-group-created',
            })
        return response


class TaxGroupUpdateView(LoginRequiredMixin, StaffPermissionRequiredMixin, UpdateView):
    permission_required = 'products.change_taxgroup'
    model = TaxGroup
    form_class = TaxGroupForm
    template_name = 'products/tax_group_form.html'
    context_object_name = 'tax_group'
    success_url = reverse_lazy('tax_group_list')


class TaxGroupDeleteView(LoginRequiredMixin, StaffPermissionRequiredMixin, DeleteView):
    permission_required = 'products.delete_taxgroup'
    model = TaxGroup
    template_name = 'products/tax_group_confirm_delete.html'
    success_url = reverse_lazy('tax_group_list')


class SuppliersListView(LoginRequiredMixin, StaffPermissionRequiredMixin, ListView):
    permission_required = 'products.view_suppliers'
    model = Suppliers
    template_name = 'products/suppliers_list.html'
    context_object_name = 'suppliers'


class SupplierCreateView(LoginRequiredMixin, StaffPermissionRequiredMixin, CreateView):
    permission_required = 'products.add_suppliers'
    model = Suppliers
    form_class = SuppliersForm
    template_name = 'products/suppliers_form.html'
    success_url = reverse_lazy('suppliers_list')

    def form_valid(self, form):
        response = super().form_valid(form)
        # Same idea as CategoryCreateView -- "+ New supplier" on the product
        # form opens this in a popup tab (?popup=1) so the in-progress
        # product form isn't lost; closing the tab and handing the new
        # supplier back via postMessage lets it show up there immediately.
        if self.request.GET.get('popup'):
            return render(self.request, 'products/_popup_close.html', {
                'created_id': self.object.pk,
                'created_name': self.object.name,
                'message_type': 'supplier-created',
            })
        return response


class SupplierUpdateView(LoginRequiredMixin, StaffPermissionRequiredMixin, UpdateView):
    permission_required = 'products.change_suppliers'
    model = Suppliers
    form_class = SuppliersForm
    template_name = 'products/suppliers_form.html'
    context_object_name = 'supplier'
    success_url = reverse_lazy('suppliers_list')


class SupplierDeleteView(LoginRequiredMixin, StaffPermissionRequiredMixin, DeleteView):
    permission_required = 'products.delete_suppliers'
    model = Suppliers
    template_name = 'products/suppliers_confirm_delete.html'
    success_url = reverse_lazy('suppliers_list')

def get_cashier_operation_from_session(request):
    return request.session.get('cashier_last_operation')


@login_required
def index(request):
    total_products = Product.objects.count()
    total_sales_count = SaleAttributes.objects.count()
    total_revenue = SaleAttributes.objects.aggregate(Sum('total_amount'))['total_amount__sum'] or 0

    recent_sales = SaleAttributes.objects.order_by('-time_of_sale')[:5]
    last_operation = get_cashier_operation_state(request)

    context = {
        'total_products': total_products,
        'total_sales_count': total_sales_count,
        'total_revenue': total_revenue,
        'recent_sales': recent_sales,
        'last_operation': last_operation if last_operation and last_operation.get('active') else None,
    }
    return render(request, 'index.html', context)


def custom_404(request, exception):
    return render(request, '404.html', status=404)