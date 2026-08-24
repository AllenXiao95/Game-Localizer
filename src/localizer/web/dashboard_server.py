"""Dashboard HTTP wrapper that injects client-side i18n and workflow UX runtimes.

The existing dashboard remains a self-contained operator surface. The i18n layer runs before the
legacy dashboard script so its DOM mutations are localized in place. The workflow UX layer runs
after the legacy script so it can safely decorate existing state/render functions without
reimplementing task, review, build, or publish authority.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from . import server as _legacy

_I18N_MARKER = "<!-- localizer-dashboard-i18n -->"
_WORKFLOW_MARKER = "<!-- localizer-dashboard-workflow-ux -->"
_I18N_CATALOG = "i18n-additions.json"
_WORKFLOW_SCRIPT = "workflow-ux.js"


def _augment_runtime(script: str) -> str:
    """Merge the prose catalog into the core runtime's PHRASES array.

    Keeping one browser-side translation engine is important: a second MutationObserver can make
    the first observer mistake translated English for a new source string, which breaks lossless
    switching back to Chinese. Catalog data is therefore merged before the script executes.
    """
    payload = json.loads((_legacy.STATIC_ROOT / _I18N_CATALOG).read_text(encoding="utf-8"))
    phrases = payload.get("phrases") or []
    rendered = []
    for pair in phrases:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("dashboard i18n catalog phrases must be [source, target] pairs")
        source, target = pair
        rendered.append(
            "    ["
            + json.dumps(str(source), ensure_ascii=False)
            + ", "
            + json.dumps(str(target), ensure_ascii=False)
            + "],"
        )
    needle = "  const PHRASES = [\n"
    if needle not in script:
        raise ValueError("dashboard i18n runtime has no PHRASES catalog insertion point")
    return script.replace(needle, needle + "\n".join(rendered) + "\n", 1)


def inject_dashboard_i18n(html: str) -> str:
    """Inject one locale engine before the legacy dashboard script."""
    if _I18N_MARKER in html:
        return html

    script = (_legacy.STATIC_ROOT / "i18n.js").read_text(encoding="utf-8")
    script = _augment_runtime(script)
    rendered = html.replace(
        'toLocaleString("zh-CN")',
        'toLocaleString(window.LocalizerI18n?.locale() || "zh-CN")',
    ).replace(
        'toLocaleTimeString("zh-CN")',
        'toLocaleTimeString(window.LocalizerI18n?.locale() || "zh-CN")',
    )
    runtime = f"{_I18N_MARKER}\n<script>\n{script}\n</script>\n"
    marker = "<script>"
    if marker in rendered:
        return rendered.replace(marker, runtime + marker, 1)
    return rendered.replace("</body>", runtime + "</body>", 1)


def inject_dashboard_workflow(html: str) -> str:
    """Inject workflow navigation after the legacy dashboard definitions are available."""
    if _WORKFLOW_MARKER in html:
        return html
    script = (_legacy.STATIC_ROOT / _WORKFLOW_SCRIPT).read_text(encoding="utf-8")
    runtime = f"{_WORKFLOW_MARKER}\n<script>\n{script}\n</script>\n"
    if "</body>" in html:
        return html.replace("</body>", runtime + "</body>", 1)
    return html + runtime


def render_dashboard_html(html: str) -> str:
    """Apply presentation-only dashboard extensions in deterministic order."""
    return inject_dashboard_workflow(inject_dashboard_i18n(html))


class _I18nHandler(_legacy._Handler):
    """Serve the normal API surface, but enhance the dashboard HTML shell."""

    def _static(self, name: str, content_type: str) -> None:
        if name != "index.html":
            super()._static(name, content_type)
            return
        path = _legacy.STATIC_ROOT / name
        if not path.is_file():
            super()._static(name, content_type)
            return
        body = render_dashboard_html(path.read_text(encoding="utf-8")).encode("utf-8")
        self._send(body, content_type)


class DashboardServer(_legacy.DashboardServer):
    """DashboardServer using the enhanced request handler.

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
