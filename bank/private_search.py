"""Keep search terms (PAN, mobile number, names) out of URLs.

A term in the address bar ends up in browser history, proxy and server logs and bookmarks.
Search boxes therefore POST the term (with the CSRF token and a hidden `_search` field naming
the term field), and `redirect_posted_search` answers with a redirect to the same page that
carries an encrypted, short-lived token instead of the term. The token only opens for the
employee who made it, so a copied link or a history entry reveals nothing to anyone else.
Pages read the term with `term()`; a plain `?q=` link (e.g. to an account number) still works.
"""
import base64
import hashlib
from urllib.parse import urlencode

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app, redirect, request
from flask_login import current_user

TOKEN_PARAM = {"q": "s", "find": "fs"}   # term field -> URL parameter carrying its token
TOKEN_TTL = 12 * 3600                     # matches the absolute session lifetime
MAX_TERM = 80


def _fernet():
    f = current_app.extensions.get("search_fernet")
    if f is None:
        secret = current_app.secret_key
        secret = secret.encode() if isinstance(secret, str) else secret
        f = Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"search-token:" + secret).digest()))
        current_app.extensions["search_fernet"] = f
    return f


def _owner():
    return str(current_user.id) if current_user.is_authenticated else "-"


def seal(term):
    """Opaque token for `term`, readable only by the current employee for TOKEN_TTL seconds."""
    term = (term or "").strip()[:MAX_TERM]
    return _fernet().encrypt(f"{_owner()}\n{term}".encode()).decode() if term else ""


def unseal(token):
    try:
        owner, _, term = _fernet().decrypt(token.encode(), ttl=TOKEN_TTL).decode().partition("\n")
    except (InvalidToken, ValueError):
        return ""
    return term if owner == _owner() else ""


def term(field="q"):
    """The search term of this request: from its token, or from a plain ?q= / ?find= link."""
    token = request.args.get(TOKEN_PARAM[field])
    if token:
        return unseal(token)
    return request.args.get(field, "").strip()[:MAX_TERM]


def args_for(value, field="q"):
    """URL arguments that carry `value` as a token, for links such as filter tabs and pagers."""
    value = (value or "").strip()
    return {TOKEN_PARAM[field]: seal(value)} if value else {}


def redirect_posted_search():
    """before_request hook (runs after the CSRF check): turn a posted search into a GET with a token."""
    if request.method != "POST":
        return None
    field = request.form.get("_search")
    if field not in TOKEN_PARAM:
        return None
    skip = {"_csrf", "_idem", "_search", "page", field, TOKEN_PARAM[field]}
    pairs = [(k, v) for k, values in request.form.lists() if k not in skip for v in values]
    pairs += list(args_for(request.form.get(field), field).items())
    return redirect(request.path + ("?" + urlencode(pairs) if pairs else ""), code=303)
