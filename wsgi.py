"""WSGI entry point for production servers (gunicorn / waitress): `gunicorn wsgi:app`."""
from bank import create_app

app = create_app()
