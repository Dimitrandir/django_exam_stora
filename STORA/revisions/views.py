from decimal import Decimal, InvalidOperation

from django.contrib.auth.decorators import login_required, permission_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import ListView

from STORA.core.mixins import StaffPermissionRequiredMixin
from STORA.products.models import Barcode, Product
from STORA.revisions.models import RevisionAttributes, RevisionItems


def _get_open_revision():
    return RevisionAttributes.objects.filter(status=RevisionAttributes.STATUS_OPEN).first()


def _serialize_item(item):
    price = item.price_at_revision if item.price_at_revision is not None else Decimal('0')
    found_sum = item.found_quantity * price
    system_sum = item.system_quantity_at_start * price
    return {
        'id': item.pk,
        'product_id': item.product_id,
        'internal_code': item.product.internal_code,
        'name': item.product.name,
        'found_quantity': float(item.found_quantity),
        'system_quantity_at_start': float(item.system_quantity_at_start),
        'quantity_diff': float(item.found_quantity - item.system_quantity_at_start),
        'price_at_revision': float(price),
        'found_sum': float(found_sum),
        'system_sum': float(system_sum),
        'sum_diff': float(found_sum - system_sum),
    }


@login_required
@permission_required('revisions.add_revisionattributes', raise_exception=True)
def revision_home(request):
    open_revision = _get_open_revision()
    return render(request, 'revisions/revision_home.html', {'open_revision': open_revision})


@require_POST
@login_required
@permission_required('revisions.add_revisionattributes', raise_exception=True)
def revision_start(request):
    # An open revision already existing (someone else started one, or this
    # same person double-clicked) is not an error -- just send them into it,
    # same as the "join" link on the landing page would. The DB constraint
    # (only_one_open_revision) is the real guard against two rows -- this
    # try/except only handles the exact-same-instant race between the check
    # above and the create() below.
    open_revision = _get_open_revision()
    if open_revision:
        return redirect('revision_view', pk=open_revision.pk)
    try:
        with transaction.atomic():
            revision = RevisionAttributes.objects.create(started_by=request.user)
    except IntegrityError:
        revision = _get_open_revision()
    return redirect('revision_view', pk=revision.pk)


@login_required
@permission_required('revisions.view_revisionattributes', raise_exception=True)
def revision_view(request, pk):
    revision = get_object_or_404(RevisionAttributes, pk=pk)

    items = revision.items.select_related('product').order_by('product__internal_code')
    items_data = [_serialize_item(item) for item in items]

    if revision.status == RevisionAttributes.STATUS_OPEN:
        products_data = list(Product.objects.values(
            'id', 'internal_code', 'name', 'delivery_price', 'sell_price', 'unit_type', 'tax_group__rate'
        ))
        barcodes_data = list(Barcode.objects.values('code', 'product_id', 'is_scale_code'))
        return render(request, 'revisions/revision_detail.html', {
            'revision': revision,
            'items_data': items_data,
            'products_data': products_data,
            'barcodes_data': barcodes_data,
            'can_complete': request.user.has_perm('revisions.change_revisionattributes'),
        })

    return render(request, 'revisions/revision_details.html', {
        'revision': revision,
        'items_data': items_data,
    })


@login_required
@permission_required('revisions.view_revisionattributes', raise_exception=True)
def revision_items_data(request, pk):
    """Polled every few seconds by revision_detail.html so a count entered
    on one computer shows up on another's screen without a manual reload."""
    revision = get_object_or_404(RevisionAttributes, pk=pk)
    items = revision.items.select_related('product').order_by('product__internal_code')
    return JsonResponse({
        'status': revision.status,
        'items': [_serialize_item(item) for item in items],
    })


@require_POST
@login_required
@permission_required('revisions.add_revisionattributes', raise_exception=True)
def revision_add_item(request, pk):
    try:
        revision = RevisionAttributes.objects.get(pk=pk, status=RevisionAttributes.STATUS_OPEN)
    except RevisionAttributes.DoesNotExist:
        return JsonResponse({'error': 'This revision is no longer open.'}, status=409)

    product_id = request.POST.get('product_id')
    product = get_object_or_404(Product, pk=product_id)

    try:
        quantity = Decimal(request.POST.get('quantity', ''))
    except InvalidOperation:
        return JsonResponse({'error': 'Invalid quantity.'}, status=400)
    if quantity <= 0:
        return JsonResponse({'error': 'Quantity must be positive.'}, status=400)

    try:
        item = RevisionItems.objects.add_count(revision, product, quantity)
    except RevisionAttributes.DoesNotExist:
        return JsonResponse({'error': 'This revision is no longer open.'}, status=409)

    item.product = product  # already have it -- skip the extra select_related query
    return JsonResponse(_serialize_item(item))


@require_POST
@login_required
@permission_required('revisions.add_revisionattributes', raise_exception=True)
def revision_remove_item(request, pk, item_id):
    try:
        revision = RevisionAttributes.objects.get(pk=pk, status=RevisionAttributes.STATUS_OPEN)
    except RevisionAttributes.DoesNotExist:
        return JsonResponse({'error': 'This revision is no longer open.'}, status=409)

    deleted, _ = RevisionItems.objects.filter(pk=item_id, revision=revision).delete()
    if not deleted:
        return JsonResponse({'error': 'That row is already gone.'}, status=404)
    return JsonResponse({'ok': True})


@require_POST
@login_required
@permission_required('revisions.change_revisionattributes', raise_exception=True)
def revision_cancel(request, pk):
    """Abandons an open revision -- no stock changes at all, since Complete
    is the only thing that ever touches Product.quantity. The counted rows
    are kept (not deleted) for reference on the read-only summary page,
    same "don't erase what happened, just mark it discarded" reasoning as
    everywhere else in the app that keeps a trail instead of hard-deleting."""
    with transaction.atomic():
        revision = get_object_or_404(
            RevisionAttributes.objects.select_for_update(), pk=pk, status=RevisionAttributes.STATUS_OPEN
        )
        revision.status = RevisionAttributes.STATUS_CANCELLED
        revision.completed_by = request.user
        revision.completed_at = timezone.now()
        revision.save(update_fields=['status', 'completed_by', 'completed_at'])

    return redirect('revision_view', pk=revision.pk)


@require_POST
@login_required
@permission_required('revisions.change_revisionattributes', raise_exception=True)
def revision_complete(request, pk):
    with transaction.atomic():
        revision = get_object_or_404(
            RevisionAttributes.objects.select_for_update(), pk=pk, status=RevisionAttributes.STATUS_OPEN
        )
        for item in revision.items.select_related('product'):
            product = Product.objects.select_for_update().get(pk=item.product_id)
            if product.quantity != item.found_quantity:
                product.quantity = item.found_quantity
                product.save(update_fields=['quantity'])

        revision.status = RevisionAttributes.STATUS_COMPLETED
        revision.completed_by = request.user
        revision.completed_at = timezone.now()
        revision.save(update_fields=['status', 'completed_by', 'completed_at'])

    return redirect('revision_view', pk=revision.pk)


class RevisionListView(LoginRequiredMixin, StaffPermissionRequiredMixin, ListView):
    permission_required = 'revisions.view_revisionattributes'
    model = RevisionAttributes
    template_name = 'revisions/revision_list.html'
    context_object_name = 'revisions'

    def get_queryset(self):
        return RevisionAttributes.objects.select_related('started_by', 'completed_by').annotate(
            item_count=Count('items')
        ).order_by('-started_at')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['revisions_data'] = [
            {
                'id': revision.pk,
                'status': revision.status,
                'started_by': str(revision.started_by),
                'started_at': revision.started_at.strftime('%Y-%m-%d %H:%M'),
                'completed_by': str(revision.completed_by) if revision.completed_by else '',
                'completed_at': revision.completed_at.strftime('%Y-%m-%d %H:%M') if revision.completed_at else '',
                'item_count': revision.item_count,
                'view_url': reverse('revision_view', kwargs={'pk': revision.pk}),
            }
            for revision in context['revisions']
        ]
        return context
