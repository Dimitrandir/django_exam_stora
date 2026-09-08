import os

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()


@register.simple_tag
def static_v(path):
    """Like {% static %}, but appends `?v=<file mtime>` so the browser
    re-fetches the file the moment it changes on disk instead of serving a
    stale cached copy -- important while actively iterating on CSS/JS."""
    url = static(path)
    absolute_path = finders.find(path)
    if not absolute_path:
        return url
    try:
        version = int(os.path.getmtime(absolute_path))
    except OSError:
        return url
    return f'{url}?v={version}'
