from django import template

register = template.Library()

@register.filter(name = 'currency')
def currency(value):
    try:
        return f'{ float(value):.2f} eur'
    except(ValueError, TypeError):
        return value


@register.filter(name='quantity_display')
def quantity_display(value, unit_type):
    """Formats a Product/SaleItems/DeliveryItems quantity per unit_type --
    weight products show 3 decimals + kg, piece products round to a whole
    number + pcs (the stored value is a Decimal either way)."""
    try:
        number = float(value)
    except (ValueError, TypeError):
        return value
    if unit_type == 'weight':
        return f'{number:.3f} kg'
    return f'{round(number)} pcs'
