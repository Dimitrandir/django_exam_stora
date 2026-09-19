web: python manage.py migrate --noinput && python manage.py collectstatic --noinput && waitress-serve --host=0.0.0.0 --port=$PORT STORA.wsgi:application
