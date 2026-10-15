"""Server-side sorting for list pages. Only whitelisted columns can be sorted on, so the
`sort` query parameter can never reach SQL as free text."""
from flask import request


def apply_sort(query, columns, default, default_desc=True):
    key = request.args.get("sort", default)
    if key not in columns:
        key = default
    direction = request.args.get("dir", "desc" if default_desc else "asc")
    if direction not in ("asc", "desc"):
        direction = "desc"
    col = columns[key]
    order = col.desc().nullslast() if direction == "desc" else col.asc().nullsfirst()
    return query.order_by(order), {"key": key, "dir": direction}
