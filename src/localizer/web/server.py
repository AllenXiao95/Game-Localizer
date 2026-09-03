"""Public dashboard server facade with fail-closed Review UI delivery.

The main implementation lives in ``server_impl``. Keeping the public module as a
small facade lets Review UI extensions be mandatory without rewriting the large
HTTP server in one shot. If a required asset is missing, ``/`` fails loudly
instead of silently serving a dashboard with a function area absent.
"""
from __future__ import annotations

from . import server_impl as _impl
from .project_history_detail import (
    project_history_coordinates,
    safe_revert_with_history_fallback,
)
from .review_recovery import project_change_history

for _name in dir(_impl):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)


_REQUIRED_REVIEW_ASSETS = (
    "review-recovery.js",
    "project-history.js",
)


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
        extensions = []
        for asset_name in _REQUIRED_REVIEW_ASSETS:
            extension = _impl.STATIC_ROOT / asset_name
            if not extension.is_file():
                self._json(
                    {
                        "error": "missing_asset",
                        "message": f"{asset_name} is required by the Review UI",
                    },
                    status=_impl.HTTPStatus.INTERNAL_SERVER_ERROR,
                )
                return
            extensions.append(extension)

        marker = b"</body>"
        if marker not in body:
            self._json(
                {"error": "invalid_asset", "message": "index.html has no </body>"},
                status=_impl.HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return

        scripts = b"".join(
            b"\n<script>\n" + extension.read_bytes() + b"\n</script>\n"
            for extension in extensions
        )
        body = body.replace(marker, scripts + marker, 1)

    self._send(body, content_type)


_original_dispatch = _impl._Handler._dispatch
_original_review_post = _impl._Handler._review_post


def _dispatch_with_project_history(self, route: str, params: dict) -> None:
    """Expose project-wide Review history and paged coordinate inspection."""
    if route not in {"/api/review/history", "/api/review/history/coordinates"}:
        _original_dispatch(self, route, params)
        return

    service = self.review
    if service is None:
        self._json(
            {"available": False, "reason": "审查视图只在回环地址上启用"},
        )
        return
    if route == "/api/review/history":
        payload = project_change_history(
            service,
            action=self._single(params, "action") or "all",
            status=self._single(params, "status") or "all",
            run_id=self._single(params, "run_id") or "",
            query=self._single(params, "q") or "",
            limit=self._int(params, "limit", 100),
            offset=self._int(params, "offset", 0),
        )
        # Operation list is a summary surface. A single audit may contain thousands
        # of coordinates; shipping those rows here makes the browser parse/render a
        # giant payload before the operator has even selected the operation. Detail
        # is fetched through the paged coordinate endpoint below.
        for operation in payload.get("operations", []):
            operation.pop("coordinates", None)
        self._json(payload)
        return
    self._json(
        project_history_coordinates(
            service,
            run_id=self._single(params, "run_id") or "",
            action=self._single(params, "action") or "",
            audit_id=self._single(params, "audit_id") or "",
            query=self._single(params, "q") or "",
            status=self._single(params, "status") or "all",
            recovery=self._single(params, "recovery") or "all",
            limit=self._int(params, "limit", 100),
            offset=self._int(params, "offset", 0),
        )
    )


def _review_post_with_historical_recovery(self, route: str, payload: dict) -> None:
    if route != "/api/review/revert":
        _original_review_post(self, route, payload)
        return
    service = self.review
    if service is None:
        self._method_not_allowed()
        return
    self._json(
        safe_revert_with_history_fallback(
            service,
            str(payload.get("run_id", "")),
            list(payload.get("decision_ids") or []),
            reason=str(payload.get("reason", "")),
            expected_log_revision=payload.get("expected_log_revision"),
        )
    )


_impl._Handler._static = _static_with_required_recovery
_impl._Handler._dispatch = _dispatch_with_project_history
_impl._Handler._review_post = _review_post_with_historical_recovery
_Handler = _impl._Handler
