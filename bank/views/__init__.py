def register_blueprints(app):
    from . import (accounts, alerts, approvals, audit, auth, customers, dashboard, deposits, kyc,
                   loans, reports, settings, tickets, transactions, users)
    for module in (auth, dashboard, customers, accounts, loans, deposits, transactions, kyc,
                   tickets, reports, alerts, audit, users, approvals, settings):
        app.register_blueprint(module.bp)
