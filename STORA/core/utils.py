import logging

logger = logging.getLogger(__name__)


def dispatch_task(task, *args, **kwargs) -> None:
    """Fires a Celery task with `.delay()`, but never lets a down/unreachable
    broker (e.g. Redis not running locally) turn into a 500 for the user.
    These are all "fire and forget" background jobs -- nothing in the
    request depends on their result, so a failed dispatch should only be
    logged, not raised.
    """
    try:
        task.delay(*args, **kwargs)
    except Exception:
        logger.warning('Could not schedule background task %s -- is the Celery broker running?',
                       getattr(task, 'name', task), exc_info=True)


def get_cashier_operation_type(path: str) -> str | None:
    if path == '/sales/add/':
        return 'sale'

    if path == '/deliveries/add/':
        return 'delivery'

    return None

def get_cashier_operation_session_key() -> str:
    return 'cashier_last_operation'


def build_cashier_operation_state(operation_type: str, path: str, data: dict | None = None,
                                  formset_data: dict | None = None, active: bool = True) -> dict:
    return {
        'type': operation_type,
        'path': path,
        'active': active,
        'data': data or {},
        'formset_data': formset_data or {},
    }


def build_restore_formset_data(formset_prefix: str, formset_initial: list[dict]) -> dict:
    restore_post_data = {
        f'{formset_prefix}-TOTAL_FORMS': str(len(formset_initial)),
        f'{formset_prefix}-INITIAL_FORMS': '0',
        f'{formset_prefix}-MIN_NUM_FORMS': '0',
        f'{formset_prefix}-MAX_NUM_FORMS': '1000',
    }

    for index, row in enumerate(formset_initial):
        for field_name, value in row.items():
            restore_post_data[f'{formset_prefix}-{index}-{field_name}'] = value

    return restore_post_data