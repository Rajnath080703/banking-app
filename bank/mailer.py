"""Outgoing email for invite and reset links. Disabled unless SMTP_HOST is configured;
callers fall back to showing the link to the administrator once."""
import os
import smtplib
import ssl
from email.message import EmailMessage

from flask import current_app


def send_mail(to, subject, body):
    host = os.environ.get("SMTP_HOST")
    if not host:
        return False
    msg = EmailMessage()
    msg["From"] = os.environ.get("SMTP_FROM", "no-reply@localbank.in")
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=10) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            if os.environ.get("SMTP_USER"):
                smtp.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
            smtp.send_message(msg)
        return True
    except (OSError, smtplib.SMTPException):
        current_app.logger.warning("mail delivery failed", extra={"event": "mail_failed"})
        return False
