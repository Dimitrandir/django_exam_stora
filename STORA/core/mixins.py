from django.contrib.auth.mixins import PermissionRequiredMixin
from django.core.exceptions import PermissionDenied


class StaffPermissionRequiredMixin(PermissionRequiredMixin):
    """Like PermissionRequiredMixin, but a logged-out visitor is sent to the
    login page (not shown a bare 403) while a logged-in employee who simply
    lacks the permission gets a real 403."""

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return super().handle_no_permission()
        raise PermissionDenied(self.get_permission_denied_message())
