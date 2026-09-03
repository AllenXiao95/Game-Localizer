"""Public Dashboard server with explicit Review routes and handler composition.

`server_impl` remains the mature transport/task implementation for compatibility,
but this module no longer mutates its `_Handler` methods at import time.  Review
history/recovery is represented by a normal handler subclass, and presentation
layers can extend `render_index_html()` without bypassing required Review assets.
"""
from __future__ import annotations

import ipaddress
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from . import server_impl as _impl
from .project_history_detail import project_history_coordinates
from .review_coordinator import CoordinatedReviewService
from .review_recovery import project_change_history, safe_revert

# Preserve historical import compatibility (`_DRAIN_MARGIN`, build_collector, etc.)
# while behavior is now provided by explicit subclasses below rather than monkey patch.
for _name in dir(_impl):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)

_REQUIRED_REVIEW_ASSETS = (
    "review-recovery.js",
    "project-history.js",
)


def _inject_required_review_assets(html: str) -> str:
    """Inject mandatory Review UI source files into the self-contained Dashboard."""
    if "</body>" not in html:
        raise ValueError("index.html has no </body>")
    blocks = []
    for asset_name in _REQUIRED_REVIEW_ASSETS:
        path = STATIC_ROOT / asset_name
        if not path.is_file():
            raise FileNotFoundError(f"{asset_name} is required by the Review UI")
        blocks.append(f"\n<script>\n{path.read_text(encoding='utf-8')}\n</script>\n")
    return html.replace("</body>", "".join(blocks) + "</body>", 1)


class _Handler(_impl._Handler):
    """Normal extension point for project Review history/recovery."""

    def render_index_html(self, html: str) -> str:
        return _inject_required_review_assets(html)

    def _static(self, name: str, content_type: str) -> None:
        path = STATIC_ROOT / name
        if not path.is_file():
            self._json(
                {"error": "missing_asset", "message": name},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        if name != "index.html":
            self._send(path.read_bytes(), content_type)
            return
        try:
            rendered = self.render_index_html(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            self._json(
                {"error": "invalid_asset", "message": str(exc)},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        self._send(rendered.encode("utf-8"), content_type)

    def _dispatch(self, route: str, params: dict) -> None:
        if route not in {"/api/review/history", "/api/review/history/coordinates"}:
            super()._dispatch(route, params)
            return
        service = self.review
        if service is None:
            self._json(
                {"available": False, "reason": "审查视图只在回环地址上启用"},
            )
            return
        if route == "/api/review/history":
            self._json(
                project_change_history(
                    service,
                    action=self._single(params, "action") or "all",
                    status=self._single(params, "status") or "all",
                    run_id=self._single(params, "run_id") or "",
                    query=self._single(params, "q") or "",
                    limit=self._int(params, "limit", 100),
                    offset=self._int(params, "offset", 0),
                )
            )
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

    def _review_post(self, route: str, payload: dict) -> None:
        if route != "/api/review/revert":
            super()._review_post(route, payload)
            return
        service = self.review
        if service is None:
            self._method_not_allowed()
            return
        self._json(
            safe_revert(
                service,
                str(payload.get("run_id", "")),
                list(payload.get("decision_ids") or []),
                reason=str(payload.get("reason", "")),
                expected_log_revision=payload.get("expected_log_revision"),
            )
        )


class DashboardServer(_impl.DashboardServer):
    """Dashboard using explicit handler/service classes and one shared TM lock."""

    handler_class = _Handler
    review_service_class = CoordinatedReviewService

    def __init__(
        self,
        collector: DashboardCollector,
        host: str = "127.0.0.1",
        port: int = 8765,
        enable_tasks: Optional[bool] = None,
    ) -> None:
        self.collector = collector
        if enable_tasks is None:
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = host.lower() == "localhost"
            enable_tasks = loopback
        self.collectors = self._variant_collectors(collector)
        self.default_variant = collector.config.active_variant
        if not self.collectors.get(self.default_variant):
            self.default_variant = ""

        self._task_executor = (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="localizer-task")
            if enable_tasks
            else None
        )
        base_workspace = self._base_workspace(collector)
        shared_profiles = TaskProfileStore(base_workspace) if enable_tasks else None
        # One lock covers task queue admission / TM maintenance / Review mutation.
        self._tm_maintenance_lock = threading.RLock()
        self.task_services = {
            name: TaskService(
                item.config,
                executor=self._task_executor,
                profiles=shared_profiles,
                is_busy=self._tasks_busy,
                maintenance_lock=self._tm_maintenance_lock,
            )
            for name, item in self.collectors.items()
        } if enable_tasks else {}
        self.tasks = self.task_services.get(self.default_variant)

        self.reviews = {
            name: self.review_service_class(
                item.config,
                output_root=item.output,
                workspace_root=item.workspace,
                is_busy=self._tasks_busy,
                mutation_lock=self._tm_maintenance_lock,
            )
            for name, item in self.collectors.items()
        } if enable_tasks else {}
        self.review = self.reviews.get(self.default_variant)
        try:
            self._httpd = ThreadingHTTPServer(
                (host, port),
                partial(
                    self.handler_class,
                    collectors=self.collectors,
                    task_services=self.task_services,
                    reviews=self.reviews,
                    default_variant=self.default_variant,
                ),
            )
        except OSError as exc:
            for service in self.task_services.values():
                service.shutdown()
            if self._task_executor is not None:
                self._task_executor.shutdown(wait=False, cancel_futures=False)
            raise DashboardBindError(
                f"无法在 {host}:{port} 启动面板：{exc}。\n"
                f"该端口可能被占用或被系统保留。换一个端口：--port 8080，"
                f"或用 --port 0 让系统分配。\n"
                f"Windows 可用 `netsh interface ipv4 show excludedportrange protocol=tcp` "
                f"查看被保留的区间。"
            ) from exc
        self._thread: Optional[threading.Thread] = None


def serve(
    config_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    repo_root: Optional[Path] = None,
    variant: Optional[str] = None,
) -> None:
    server = DashboardServer(
        build_collector(config_path, repo_root, variant=variant), host, port
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
