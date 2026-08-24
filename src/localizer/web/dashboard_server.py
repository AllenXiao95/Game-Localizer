"""Dashboard HTTP wrapper that injects the client-side i18n runtime.

The existing dashboard is intentionally kept as a single self-contained operator surface. Rather
than duplicating its task/review/build behavior for each language, this wrapper injects a locale
layer into the rendered HTML. The locale layer only changes presentation text and attributes; it
never rebuilds task forms or review editors, so switching language cannot discard operator state.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import server as _legacy

_I18N_MARKER = "<!-- localizer-dashboard-i18n -->"
_I18N_SCRIPTS = ("i18n.js", "i18n-additions.js")


def inject_dashboard_i18n(html: str) -> str:
    """Inject locale runtimes into one dashboard document.

    The legacy page formats numbers and the refresh clock with a hard-coded ``zh-CN`` locale.
    Replace only those formatting calls, then run the i18n runtimes *before* the dashboard's own
    script. The core runtime owns language/state mechanics; the additions layer covers long-form
    workflow and review guidance without introducing a second page state.
    """
    if _I18N_MARKER in html:
        return html

    scripts = [
        (_legacy.STATIC_ROOT / name).read_text(encoding="utf-8")
        for name in _I18N_SCRIPTS
    ]
    rendered = html.replace(
        'toLocaleString("zh-CN")',
        'toLocaleString(window.LocalizerI18n?.locale() || "zh-CN")',
    ).replace(
        'toLocaleTimeString("zh-CN")',
        'toLocaleTimeString(window.LocalizerI18n?.locale() || "zh-CN")',
    )
    script_blocks = "\n".join(f"<script>\n{script}\n</script>" for script in scripts)
    runtime = f"{_I18N_MARKER}\n{script_blocks}\n"
    marker = "<script>"
    if marker in rendered:
        return rendered.replace(marker, runtime + marker, 1)
    return rendered.replace("</body>", runtime + "</body>", 1)


class _I18nHandler(_legacy._Handler):
    """Serve the normal API surface, but localize the dashboard HTML shell."""

    def _static(self, name: str, content_type: str) -> None:
        if name != "index.html":
            super()._static(name, content_type)
            return
        path = _legacy.STATIC_ROOT / name
        if not path.is_file():
            super()._static(name, content_type)
            return
        body = inject_dashboard_i18n(path.read_text(encoding="utf-8")).encode("utf-8")
        self._send(body, content_type)


class DashboardServer(_legacy.DashboardServer):
    """DashboardServer using the i18n-aware request handler.

    The base class constructs its ``ThreadingHTTPServer`` synchronously. Temporarily replacing the
    module-level handler during that construction makes the resulting ``partial`` capture our
    subclass without changing the mature routing/task/review implementation.
    """

    def __init__(
        self,
        collector: _legacy.DashboardCollector,
        host: str = "127.0.0.1",
        port: int = 8765,
        enable_tasks: Optional[bool] = None,
    ) -> None:
        original = _legacy._Handler
        _legacy._Handler = _I18nHandler
        try:
            super().__init__(collector, host=host, port=port, enable_tasks=enable_tasks)
        finally:
            _legacy._Handler = original


def serve(
    config_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    repo_root: Optional[Path] = None,
    variant: Optional[str] = None,
) -> None:
    server = DashboardServer(
        _legacy.build_collector(config_path, repo_root, variant=variant), host, port
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
