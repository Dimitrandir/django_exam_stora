from STORA.core.utils import get_cashier_operation_session_key


def get_cashier_operation_state(request) -> dict | None:
    return request.session.get(get_cashier_operation_session_key())


def set_cashier_operation_state(request, state: dict) -> None:
    request.session[get_cashier_operation_session_key()] = state
    request.session.modified = True


def clear_cashier_operation_state(request) -> None:
    request.session.pop(get_cashier_operation_session_key(), None)
    request.session.modified = True


def extract_formset_state(post_data, prefix: str) -> dict:
    total_forms = int(post_data.get(f'{prefix}-TOTAL_FORMS', 0))
    forms = []

    for index in range(total_forms):
        forms.append({
            'delivery_item': post_data.get(f'{prefix}-{index}-delivery_item', ''),
            'sale_item': post_data.get(f'{prefix}-{index}-sale_item', ''),
            'product_code': post_data.get(f'{prefix}-{index}-product_code', ''),
            'product_name': post_data.get(f'{prefix}-{index}-product_name', ''),
            'delivery_quantity': post_data.get(f'{prefix}-{index}-delivery_quantity', ''),
            'sale_quantity': post_data.get(f'{prefix}-{index}-sale_quantity', ''),
            'delivery_price': post_data.get(f'{prefix}-{index}-delivery_price', ''),
            'unit_price': post_data.get(f'{prefix}-{index}-unit_price', ''),
            'line_total': post_data.get(f'{prefix}-{index}-line_total', ''),
            'DELETE': post_data.get(f'{prefix}-{index}-DELETE', ''),
            'price_at_delivery': post_data.get(f'{prefix}-{index}-price_at_delivery', ''),
            'price_at_sale': post_data.get(f'{prefix}-{index}-price_at_sale', ''),
            'total_price_row': post_data.get(f'{prefix}-{index}-total_price_row', ''),
        })

    return {
        'TOTAL_FORMS': total_forms,
        'forms': forms,
    }
