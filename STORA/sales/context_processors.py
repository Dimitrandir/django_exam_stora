from django.conf import settings
from django.urls import reverse
from django.utils.translation import gettext as _

from . import fiscal


def fiscal_bridge(request):
    """Bridge mode only (FISCAL_MODE=bridge): gives base.html what it needs
    to load static/js/fiscal-bridge.js, plus any receipt waiting to be
    printed (queued by a view with fiscal.queue_bridge_job).

    The pending job is a callable, so it's only taken out of the session
    when a page actually renders it -- base.html reads it once with
    {% with %}. Any other template rendered with this context (an AJAX
    partial, say) leaves it alone."""
    if not fiscal.bridge_mode():
        return {}

    session = getattr(request, 'session', None)

    def pending_job():
        return session.pop(fiscal.BRIDGE_JOB_SESSION_KEY, None) if session is not None else None

    return {
        'fiscal_bridge_job': pending_job,
        'fiscal_bridge_config': {
            'bridgeUrl': settings.FISCAL_BRIDGE_URL.rstrip('/'),
            'resultUrl': reverse('fiscal_bridge_result'),
            'messages': {
                'printing': _('Printing the fiscal receipt...'),
                'printed': _('Fiscal receipt printed.'),
                'failed': _('Fiscal receipt was NOT printed:'),
                'unreachable': _(
                    'The fiscal bridge on this computer is not running or not reachable. '
                    'The sale is saved -- start the bridge and use Retry Fiscal Print.'
                ),
                'reportDone': _('Done -- check the printer.'),
            },
        },
    }
