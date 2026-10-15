"""Start the Local Bank Customer 360 & Operations Platform on this machine.

    docker compose up -d     (starts PostgreSQL, first time and after a reboot)
    python run.py            -> http://127.0.0.1:5000

Settings come from the .env file. On start, the database schema is brought up to date with the
migrations (as the owner role, BANK_DB_OWNER_URL), reference data is ensured, and the app is
served by waitress connected as the restricted runtime role (DATABASE_URL). No sample data is added.
For a production deployment use the container stack in deploy/ instead.
"""
import os

from dotenv import load_dotenv

load_dotenv()

from bank.bootstrap import prepare_database  # noqa: E402

if os.environ.get("BANK_DB_OWNER_URL"):
    prepare_database()

from bank import create_app  # noqa: E402
from bank.seed import SUPER_ADMIN_EMAIL  # noqa: E402

app = create_app()

if __name__ == "__main__":
    from waitress import serve

    host = os.environ.get("BANK_HOST", "127.0.0.1")
    port = int(os.environ.get("BANK_PORT", "5000"))
    print(f"\n  Local Bank Ops running at http://{host}:{port}")
    print(f"  Database: PostgreSQL ({app.config['SQLALCHEMY_DATABASE_URI'].split('@')[-1]})")
    print(f"  Super Admin: {SUPER_ADMIN_EMAIL}\n")
    serve(app, host=host, port=port, threads=8, ident=None)
