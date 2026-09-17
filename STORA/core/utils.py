import logging
import socket
from urllib.parse import urlparse

from django.conf import settings
from django.db.models import Q

logger = logging.getLogger(__name__)

# How long to wait for the broker probe in dispatch_task() below -- a real,
# reachable Redis answers in well under this; an unreachable one (pilot
# machine, no Redis running -- see DEPLOYMENT.md) is refused near-instantly
# by the OS on both Linux and Windows. Deliberately NOT relying on Celery's
# own connection-retry settings for this: `Connection.connect()` in kombu
# has a hardcoded internal retry with a 2-second sleep between attempts
# that no documented Celery setting (broker_connection_timeout,
# broker_transport_options, broker_connection_retry, task_publish_retry --
# all tried) actually overrides, so task.delay() to a down broker could
# stall a request for several real seconds no matter how Celery is
# configured. Probing the raw socket ourselves sidesteps that entirely.
_BROKER_PROBE_TIMEOUT = 0.2


def multi_token_icontains_q(query: str, fields: list[str]) -> Q:
    """Splits `query` on whitespace and requires EVERY token to appear in
    at least one of `fields` (icontains) -- so "вода пред" matches "Изворна
    вода Предела" (both words present, any order/position), not just a
    name containing that exact two-word phrase. A query with no spaces
    (e.g. a scanned barcode/code) collapses to a single token, behaving
    exactly like the old single-substring check -- this is a strict
    superset, not a behavior change, for anything without whitespace.
    """
    combined = Q()
    for token in query.split():
        token_q = Q()
        for field in fields:
            token_q |= Q(**{f'{field}__icontains': token})
        combined &= token_q
    return combined


def _broker_is_reachable() -> bool:
    """Cheap TCP probe of CELERY_BROKER_URL's host:port -- see
    _BROKER_PROBE_TIMEOUT above for why this exists instead of trusting
    Celery/Kombu's own timeout settings."""
    parsed = urlparse(settings.CELERY_BROKER_URL)
    if not parsed.hostname or not parsed.port:
        return True  # unrecognized URL shape -- don't block dispatch on it
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=_BROKER_PROBE_TIMEOUT):
            return True
    except OSError:
        return False


def dispatch_task(task, *args, **kwargs) -> None:
    """Fires a Celery task with `.delay()`, but never lets a down/unreachable
    broker (e.g. Redis not running locally) turn into a 500 -- or a multi-
    second stall -- for the user. These are all "fire and forget" background
    jobs -- nothing in the request depends on their result, so a failed (or
    skipped) dispatch should only be logged, not raised.
    """
    if not _broker_is_reachable():
        logger.warning('Could not schedule background task %s -- broker unreachable (is the Celery broker running?)',
                       getattr(task, 'name', task))
        return
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

    if path == '/deliveries/write-off/add/':
        return 'write_off'

    if path == '/deliveries/scrap/add/':
        return 'scrap'

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