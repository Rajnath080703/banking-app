"""Structured JSON logs (one object per line) with the request id on every record.

Only metadata is logged: method, path (never the query string, which can carry a searched
PAN or mobile number), status, duration and employee id. No customer data, no passwords."""
import json
import logging
import sys
from datetime import datetime, timezone

from flask import g, has_request_context

_STD = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "level": record.levelname,
                "logger": record.name, "msg": record.getMessage()}
        if has_request_context():
            data["request_id"] = getattr(g, "request_id", None)
        for key, value in vars(record).items():
            if key not in _STD and not key.startswith("_"):
                data[key] = value
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


def configure_logging(app):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    app.logger.handlers[:] = [handler]
    app.logger.setLevel(logging.INFO)
    app.logger.propagate = False
    app.logger.disabled = False  # a library's logging.config call must never silence the audit/request log
    # Werkzeug's own access log would print query strings; our request log replaces it.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
