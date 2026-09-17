import json
from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from django.views.generic import ListView

from STORA.core.mixins import StaffPermissionRequiredMixin
from STORA.products.models import Category, Product, Suppliers
from STORA.pricelists.forms import PriceListForm
from STORA.pricelists.models import PriceList, PriceListRule
from STORA.pricelists.services import price_without_vat, rules_for_price_list


def _rules_data(price_list):
    """Serializes an existing list's rules back into the same shape the
    create/edit page's JS builds client-side -- lets the Edit page seed its
    Tabulator grid with what's already there."""
    rows = []
    for rule in price_list.rules.select_related('product', 'category', 'supplier'):
        target = {
            PriceListRule.SCOPE_PRODUCT: rule.product,
            PriceListRule.SCOPE_CATEGORY: rule.category,
            PriceListRule.SCOPE_SUPPLIER: rule.supplier,
        }[rule.scope_type]
        rows.append({
            'scope_type': rule.scope_type,
            'target_id': target.pk,
            'target_name': str(target),
            'discount_percent': float(rule.discount_percent) if rule.discount_percent is not None else None,
            'fixed_price': float(rule.fixed_price) if rule.fixed_price is not None else None,
        })
    return rows


def _parse_rules(raw_json):
    """Turns the posted rules_json (list of plain dicts from the JS grid)
    into unsaved PriceListRule instances, running full_clean() on each so
    the same scope/price validation as the model applies here too. Raises
    ValidationError (message aggregates every bad row) if anything's off --
    the view catches it and re-renders with the error, same as any other
    form failure in this app."""
    try:
        rows = json.loads(raw_json or '[]')
    except (TypeError, ValueError):
        raise ValidationError('Could not read the submitted rules.')

    if not rows:
        raise ValidationError('Add at least one product, category, or supplier to this price list.')

    rules = []
    errors = []
    for index, row in enumerate(rows, start=1):
        rule = PriceListRule(scope_type=row.get('scope_type'))
        target_id = row.get('target_id')
        if rule.scope_type == PriceListRule.SCOPE_PRODUCT:
            rule.product_id = target_id
        elif rule.scope_type == PriceListRule.SCOPE_CATEGORY:
            rule.category_id = target_id
        elif rule.scope_type == PriceListRule.SCOPE_SUPPLIER:
            rule.supplier_id = target_id

        discount_percent = row.get('discount_percent')
        fixed_price = row.get('fixed_price')
        try:
            rule.discount_percent = Decimal(str(discount_percent)) if discount_percent not in (None, '') else None
            rule.fixed_price = Decimal(str(fixed_price)) if fixed_price not in (None, '') else None
        except InvalidOperation:
            errors.append(f'Row {index}: invalid number.')
            continue

        try:
            rule.clean()
        except ValidationError as exc:
            errors.append(f'Row {index}: {"; ".join(exc.messages)}')
            continue
        rules.append(rule)

    if errors:
        raise ValidationError(errors)
    return rules


def _picker_context():
    return {
        'categories_data': list(Category.objects.values('id', 'name')),
        'suppliers_data': list(Suppliers.objects.values('id', 'name')),
    }


@login_required
@permission_required('pricelists.add_pricelist', raise_exception=True)
def price_list_create(request):
    if request.method == 'POST':
        form = PriceListForm(request.POST)
        rules_error = None
        if form.is_valid():
            try:
                rules = _parse_rules(request.POST.get('rules_json'))
            except ValidationError as exc:
                rules_error = exc.messages
            else:
                with transaction.atomic():
                    price_list = form.save(commit=False)
                    price_list.created_by = request.user
                    price_list.save()
                    for rule in rules:
                        rule.price_list = price_list
                    PriceListRule.objects.bulk_create(rules)
                return redirect('price_list_detail', pk=price_list.pk)
        context = {'form': form, 'rules_error': rules_error, 'existing_rules': [], **_picker_context()}
        return render(request, 'pricelists/price_list_form.html', context)

    form = PriceListForm()
    context = {'form': form, 'rules_error': None, 'existing_rules': [], **_picker_context()}
    return render(request, 'pricelists/price_list_form.html', context)


@login_required
@permission_required('pricelists.change_pricelist', raise_exception=True)
def price_list_edit(request, pk):
    price_list = get_object_or_404(PriceList, pk=pk)

    if request.method == 'POST':
        form = PriceListForm(request.POST, instance=price_list)
        rules_error = None
        if form.is_valid():
            try:
                rules = _parse_rules(request.POST.get('rules_json'))
            except ValidationError as exc:
                rules_error = exc.messages
            else:
                with transaction.atomic():
                    form.save()
                    price_list.rules.all().delete()
                    for rule in rules:
                        rule.price_list = price_list
                    PriceListRule.objects.bulk_create(rules)
                return redirect('price_list_detail', pk=price_list.pk)
        context = {
            'form': form, 'rules_error': rules_error, 'price_list': price_list,
            'existing_rules': _rules_data(price_list), **_picker_context(),
        }
        return render(request, 'pricelists/price_list_form.html', context)

    form = PriceListForm(instance=price_list)
    context = {
        'form': form, 'rules_error': None, 'price_list': price_list,
        'existing_rules': _rules_data(price_list), **_picker_context(),
    }
    return render(request, 'pricelists/price_list_form.html', context)


@login_required
@permission_required('pricelists.view_pricelist', raise_exception=True)
def price_list_detail(request, pk):
    price_list = get_object_or_404(PriceList, pk=pk)
    winning_rule_by_product = rules_for_price_list(price_list)
    products = Product.objects.filter(pk__in=winning_rule_by_product.keys()).select_related(
        'tax_group'
    ).order_by('internal_code')

    items_data = []
    for product in products:
        rule = winning_rule_by_product[product.pk]
        new_price = rule.effective_price(product.sell_price)
        items_data.append({
            'product_id': product.pk,
            'internal_code': product.internal_code,
            'name': product.name,
            'scope_type': rule.scope_type,
            'base_price': float(product.sell_price),
            'base_price_no_vat': float(price_without_vat(product.sell_price, product)),
            'discount_percent': float(rule.discount_percent) if rule.discount_percent is not None else None,
            'new_price': float(new_price),
            'new_price_no_vat': float(price_without_vat(new_price, product)),
        })

    return render(request, 'pricelists/price_list_detail.html', {
        'price_list': price_list,
        'items_data': items_data,
    })


@require_POST
@login_required
@permission_required('pricelists.change_pricelist', raise_exception=True)
def price_list_delete(request, pk):
    price_list = get_object_or_404(PriceList, pk=pk)
    price_list.is_deleted = True
    price_list.save(update_fields=['is_deleted'])
    return redirect('price_list_list')


class PriceListListView(LoginRequiredMixin, StaffPermissionRequiredMixin, ListView):
    permission_required = 'pricelists.view_pricelist'
    model = PriceList
    template_name = 'pricelists/price_list_list.html'
    context_object_name = 'price_lists'

    def get_queryset(self):
        queryset = PriceList.objects.select_related('created_by')
        if self.request.GET.get('show_deleted') != '1':
            queryset = queryset.filter(is_deleted=False)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        today = timezone.localdate()
        context['price_lists_data'] = [
            {
                'id': price_list.pk,
                'name': price_list.name,
                'priority': price_list.priority,
                'start_date': price_list.start_date.isoformat(),
                'end_date': price_list.end_date.isoformat(),
                'status': 'Active' if price_list.start_date <= today <= price_list.end_date else (
                    'Upcoming' if price_list.start_date > today else 'Expired'
                ),
                'is_deleted': price_list.is_deleted,
                'created_by': str(price_list.created_by),
                'view_url': reverse('price_list_detail', kwargs={'pk': price_list.pk}),
                'edit_url': reverse('price_list_edit', kwargs={'pk': price_list.pk}),
            }
            for price_list in context['price_lists']
        ]
        context['show_deleted'] = self.request.GET.get('show_deleted') == '1'
        return context


@require_GET
@login_required
@permission_required('pricelists.view_pricelist', raise_exception=True)
def price_list_check_conflict(request):
    """Called by the create/edit page's JS right after a product/category/
    supplier is added to the grid -- looks for any OTHER currently-relevant
    (in-period, non-deleted) price list that already has a rule reaching
    the same target, so the person editing sees it immediately instead of
    discovering it later at checkout. Informational only -- doesn't block
    adding the row."""
    scope_type = request.GET.get('scope_type')
    target_id = request.GET.get('target_id')
    exclude_pk = request.GET.get('exclude_pk')

    field_by_scope = {
        PriceListRule.SCOPE_PRODUCT: 'product_id',
        PriceListRule.SCOPE_CATEGORY: 'category_id',
        PriceListRule.SCOPE_SUPPLIER: 'supplier_id',
    }
    field = field_by_scope.get(scope_type)
    if not field or not target_id:
        return JsonResponse({'conflicts': []})

    today = timezone.localdate()
    rules = PriceListRule.objects.filter(
        scope_type=scope_type, **{field: target_id},
        price_list__is_deleted=False,
        price_list__start_date__lte=today, price_list__end_date__gte=today,
    ).select_related('price_list')
    if exclude_pk:
        rules = rules.exclude(price_list_id=exclude_pk)

    return JsonResponse({'conflicts': [
        {
            'price_list_id': rule.price_list_id,
            'price_list_name': rule.price_list.name,
            'discount_percent': float(rule.discount_percent) if rule.discount_percent is not None else None,
            'fixed_price': float(rule.fixed_price) if rule.fixed_price is not None else None,
        }
        for rule in rules
    ]})
