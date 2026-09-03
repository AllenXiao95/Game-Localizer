"""Dashboard presentation layer: i18n + workflow UX over the core Review server.

Presentation now composes through ``render_index_html``.  It does not replace module
globals or bypass the core server's mandatory Review assets/routes.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from . import server as _core

_I18N_MARKER = "<!-- localizer-dashboard-i18n -->"
_WORKFLOW_MARKER = "<!-- localizer-dashboard-workflow-ux -->"
_I18N_CATALOGS = (
    "i18n-additions.json",
    "workflow-i18n.json",
    "workflow-publish-i18n.json",
)
_WORKFLOW_SCRIPTS = (
    "workflow-ux.js",
    "workflow-locale-bridge.js",
    "workflow-publish-ux.js",
)


def _augment_runtime(script: str) -> str:
    """Merge all prose catalogs into the core runtime's PHRASES array."""
    phrases = []
    for catalog in _I18N_CATALOGS:
        payload = json.loads((_core.STATIC_ROOT / catalog).read_text(encoding="utf-8"))
        phrases.extend(payload.get("phrases") or [])
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
    """Inject one locale engine before the first Dashboard script."""
    if _I18N_MARKER in html:
        return html
    script = (_core.STATIC_ROOT / "i18n.js").read_text(encoding="utf-8")
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
    """Inject workflow UX after the core Dashboard/Review definitions."""
    if _WORKFLOW_MARKER in html:
        return html
    scripts = [
        (_core.STATIC_ROOT / name).read_text(encoding="utf-8")
        for name in _WORKFLOW_SCRIPTS
    ]
    blocks = "\n".join(f"<script>\n{script}\n</script>" for script in scripts)
    runtime = f"{_WORKFLOW_MARKER}\n{blocks}\n"
    if "</body>" in html:
        return html.replace("</body>", runtime + "</body>", 1)
    return html + runtime


def render_dashboard_html(html: str) -> str:
    """Apply presentation-only extensions in deterministic order."""
    return inject_dashboard_workflow(inject_dashboard_i18n(html))


class _I18nHandler(_core._Handler):
    """Presentation hook; all routing/static authority remains in the core handler."""

    def render_index_html(self, html: str) -> str:
        return render_dashboard_html(super().render_index_html(html))


class DashboardServer(_core.DashboardServer):
    """Core DashboardServer with a normal presentation handler subclass."""

    handler_class = _I18nHandler


def serve(
    config_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    repo_root: Optional[Path] = None,
    variant: Optional[str] = None,
) -> None:
    server = DashboardServer(
        _core.build_collector(config_path, repo_root, variant=variant), host, port
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
