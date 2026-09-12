from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.postgres.search import TrigramSimilarity
from django.db.models.functions import Greatest
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views import View

from STORA.core.session_service import clear_cashier_operation_state
from STORA.core.utils import multi_token_icontains_q


def clear_cashier_operation(request):
    clear_cashier_operation_state(request)
    return redirect('sales_list')


class GlobalSearchView(LoginRequiredMixin, View):
    """Navbar search: looks across products, suppliers, categories and
    employees at once. Uses Postgres trigram similarity (pg_trgm) for
    ranking -- tolerates typos/partial matches and stays fast at scale via
    the GIN trigram indexes on each searched field (see the apps' models).

    Only `LoginRequiredMixin`, no extra permission check: every logged-in
    role already has read access to all four of these (products/suppliers/
    categories via signals.py, employees via EmployeeListView having no
    permission gate) -- this endpoint doesn't expose anything new.
    """

    RESULTS_PER_CATEGORY = 5
    MIN_QUERY_LENGTH = 2

    def get(self, request):
        query = request.GET.get('q', '').strip()
        if len(query) < self.MIN_QUERY_LENGTH:
            return JsonResponse({'products': [], 'suppliers': [], 'categories': [], 'employees': []})

        return JsonResponse({
            'products': self._search_products(query),
            'suppliers': self._search_suppliers(query),
            'categories': self._search_categories(query),
            'employees': self._search_employees(query),
        })

    def _search_products(self, query):
        from STORA.products.models import Product

        results = (
            Product.objects
            .filter(multi_token_icontains_q(query, ['name', 'internal_code', 'barcode__code']))
            .distinct()
            .annotate(similarity=Greatest(
                TrigramSimilarity('name', query), TrigramSimilarity('internal_code', query),
            ))
            .order_by('-similarity', 'name')[:self.RESULTS_PER_CATEGORY]
        )
        return [
            {'label': f'{p.internal_code} — {p.name}', 'url': reverse('product_details', kwargs={'pk': p.pk})}
            for p in results
        ]

    def _search_suppliers(self, query):
        from STORA.products.models import Suppliers

        results = (
            Suppliers.objects
            .filter(multi_token_icontains_q(query, ['name', 'bulstat']))
            .annotate(similarity=TrigramSimilarity('name', query))
            .order_by('-similarity', 'name')[:self.RESULTS_PER_CATEGORY]
        )
        return [
            {'label': supplier.name, 'url': reverse('suppliers_edit', kwargs={'pk': supplier.pk})}
            for supplier in results
        ]

    def _search_categories(self, query):
        from STORA.products.models import Category

        results = (
            Category.objects
            .filter(multi_token_icontains_q(query, ['name']))
            .annotate(similarity=TrigramSimilarity('name', query))
            .order_by('-similarity', 'name')[:self.RESULTS_PER_CATEGORY]
        )
        return [
            {'label': category.name, 'url': reverse('category_edit', kwargs={'pk': category.pk})}
            for category in results
        ]

    def _search_employees(self, query):
        from STORA.accounts.models import Employee

        results = (
            Employee.objects
            .filter(multi_token_icontains_q(query, ['first_name', 'last_name', 'username']))
            .annotate(similarity=Greatest(
                TrigramSimilarity('first_name', query),
                TrigramSimilarity('last_name', query),
                TrigramSimilarity('username', query),
            ))
            .order_by('-similarity', 'username')[:self.RESULTS_PER_CATEGORY]
        )
        return [
            {
                'label': f'{employee.first_name} {employee.last_name} ({employee.username})',
                'url': reverse('employee_details', kwargs={'pk': employee.pk}),
            }
            for employee in results
        ]