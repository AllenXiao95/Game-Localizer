"""Public dashboard server facade with fail-closed Recovery UI delivery.

The main implementation lives in ``server_impl``.  Keeping the public module as a
small facade lets Recovery UI delivery be mandatory without rewriting the large
HTTP server in one shot.  If the Recovery asset is missing, ``/`` fails loudly
instead of silently serving a dashboard with the recovery feature absent.
"""
from __future__ import annotations

from . import server_impl as _impl

# Preserve the public/private surface of the historical module.  A few tests and
# internal callers import underscore names (for example ``_DRAIN_MARGIN``), so this
# is intentionally broader than ``from ... import *``.
for _name in dir(_impl):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)


def _static_with_required_recovery(self, name: str, content_type: str) -> None:
    path = _impl.STATIC_ROOT / name
    if not path.is_file():
        self._json(
            {"error": "missing_asset", "message": name},
            status=_impl.HTTPStatus.INTERNAL_SERVER_ERROR,
        )
        return

    body = path.read_bytes()
    if name == "index.html":
        extension = _impl.STATIC_ROOT / "review-recovery.js"
        if not extension.is_file():
            self._json(
                {
                    "error": "missing_asset",
                    "message": "review-recovery.js is required by the Review Recovery UI",
                },
                status=_impl.HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        marker = b"</body>"
        if marker not in body:
            self._json(
                {"error": "invalid_asset", "message": "index.html has no </body>"},
                status=_impl.HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        script = b"\n<script>\n" + extension.read_bytes() + b"\n</script>\n"
        body = body.replace(marker, script + marker, 1)

    self._send(body, content_type)


# DashboardServer builds the request handler from the class object defined in the
# implementation module.  Patch that exact class so every public entrypoint
# (console script, ``python -m``, tests) uses mandatory Recovery delivery.
_impl._Handler._static = _static_with_required_recovery
_Handler = _impl._Handler
