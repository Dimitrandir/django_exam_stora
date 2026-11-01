#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys


def _redirect_missing_console():
    """pythonw.exe (Python without a console window -- what the backup task
    runs on a cashier PC whose Windows account has no password, see
    DEPLOYMENT.md section 8) starts with sys.stdout/sys.stderr set to None.
    The first thing a command prints then crashes with "'NoneType' object
    has no attribute 'write'" (Task Scheduler shows just 0x1). Send them to
    logs/background_commands.log instead, so the output is kept too."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    stream = open(os.path.join(log_dir, 'background_commands.log'), 'a', encoding='utf-8')
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def main():
    """Run administrative tasks."""
    _redirect_missing_console()
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'STORA.settings')
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
