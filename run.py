"""Start the Local Bank Customer 360 & Operations Platform.

    python run.py            -> http://127.0.0.1:5000

On first start the database is created with the Super Admin account and demo data.
"""
import os

from bank import create_app
from bank.extensions import db
from bank.seed import SUPER_ADMIN_EMAIL, ensure_core, seed_demo
from bank.services import scan_scheduled_alerts

app = create_app()

with app.app_context():
    ensure_core()
    if os.environ.get("BANK_DEMO_DATA", "1") == "1" and seed_demo():
        print("Demo data loaded.")
    scan_scheduled_alerts(force=True)
    db.session.remove()

if __name__ == "__main__":
    host = os.environ.get("BANK_HOST", "127.0.0.1")
    port = int(os.environ.get("BANK_PORT", "5000"))
    cert, key = os.environ.get("BANK_SSL_CERT"), os.environ.get("BANK_SSL_KEY")
    ssl_context = (cert, key) if cert and key else None
    scheme = "https" if ssl_context else "http"
    print(f"\n  Local Bank Ops running at {scheme}://{host}:{port}")
    print(f"  Super Admin: {SUPER_ADMIN_EMAIL}\n")
    app.run(host=host, port=port, debug=False, ssl_context=ssl_context)
