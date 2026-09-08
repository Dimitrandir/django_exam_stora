from decimal import Decimal

from celery import shared_task
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone


@shared_task(ignore_result=True)
def log_sale_completed(sale_id: int) -> None:
    print(f'Sale {sale_id} completed successfully.')


@shared_task(ignore_result=True)
def backfill_recipe_ingredient_stock(product_id: int) -> None:
    """A product can be sold as a plain item for a while and only later get
    turned into a recipe. Its past sales already deducted from its own
    (now-irrelevant) stock and never touched the ingredients. This walks
    every historical SaleItems row for that product and retroactively
    deducts the ingredients, so on-hand ingredient stock reflects what was
    actually used all along.

    Runs once per recipe: guarded by `ingredients_backfilled_at`, so saving
    the recipe again later (e.g. editing quantities) does not re-run it and
    double-deduct. Only future sales are affected by a later edit.
    """
    from STORA.products.models import Product
    from STORA.sales.models import SaleItems

    with transaction.atomic():
        product = Product.objects.select_for_update().get(pk=product_id)

        if not product.is_recipe or product.ingredients_backfilled_at is not None:
            return

        total_sold = SaleItems.objects.filter(sale_item=product).aggregate(
            total=Sum('sale_quantity')
        )['total'] or Decimal('0')

        if total_sold > 0:
            ingredient_links = list(product.recipe_ingredients.select_related('ingredient'))
            ingredient_ids = [link.ingredient_id for link in ingredient_links]
            locked_ingredients = {
                ingredient.pk: ingredient
                for ingredient in Product.objects.select_for_update().filter(pk__in=ingredient_ids)
            }
            for link in ingredient_links:
                ingredient = locked_ingredients[link.ingredient_id]
                ingredient.quantity -= link.quantity * total_sold
                ingredient.save(update_fields=['quantity'])

        product.ingredients_backfilled_at = timezone.now()
        product.save(update_fields=['ingredients_backfilled_at'])