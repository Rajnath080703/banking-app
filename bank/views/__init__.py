def register_blueprints(app):
    from . import (accounting, accounts, alerts, approvals, audit, auth, customers, dashboard, deposits, health, kyc,
                   loans, reports, settings, tickets, transactions, users)
    for module in (health, auth, dashboard, customers, accounts, loans, deposits, transactions, kyc,
                   tickets, reports, alerts, audit, users, approvals, settings, accounting):
        app.register_blueprint(module.bp)
