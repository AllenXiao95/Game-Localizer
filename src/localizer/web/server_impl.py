"""观测面板与受控本地任务启动器的 HTTP 服务。

刻意只用标准库：面板不应该给这个项目引入 Web 框架依赖，也要能在没装可选依赖的
机器上打开。默认只绑定 127.0.0.1。写接口仅用于保存无凭据任务预设、启动/增量重建既有
ProjectRunner，以及 QA 缺陷的单人定点修复；本模块仍不提供多人审核流程、stage 修改或
凭据编辑能力。
"""
from __future__ import annotations

import ipaddress
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from localizer.config import load_project_config

from localizer.application.review_log import LogRevisionMismatch

from .collector import DashboardCollector
from .review import ReviewConflict, ReviewService, ReviewUnavailable
from .review_recovery import recovery_operations, safe_revert
from .tasks import TaskProfileStore, TaskService

STATIC_ROOT = Path(__file__).resolve().parent / "static"

# 允许 POST 的路由。白名单是这一层唯一的准入判据 —— 加路由必须在这里登记。
_POST_ROUTES = {
    "/api/task-profiles",
    "/api/tasks/preflight",
    "/api/tasks/confirm-stale",
    "/api/tasks/run",
    "/api/tasks/rebuild",
    "/api/tasks/publish",
    "/api/review/recheck",
    "/api/review/decisions",
    "/api/review/commit",
    "/api/review/unify",
    "/api/review/unify-majorities",
    "/api/review/glossary-exclude",
    "/api/review/revert",
}

# 审查提交一次最多 100 条编辑，每条译文可以很长；默认 64 KB 装不下。
_BODY_LIMITS = {
    "/api/review/commit": 1 << 20,
    "/api/review/decisions": 1 << 20,
    "/api/review/recheck": 1 << 20,
}


class _PayloadTooLarge(ValueError):
    """请求体超限。单独一类，好映射成 413 而不是 400。"""


# 超出上限之后还愿意读走多少字节，**只为了让 413 能送达**。
# 不读就直接关连接，客户端会在还没写完 body 时撞上 RST，看到的是
# ConnectionReset 而不是我们的错误响应；不设上限地读，又等于把攻击者
# 想让我们读的字节数全读完。这个余量只覆盖「稍微超一点」的正常客户端。
_DRAIN_MARGIN = 1 << 16


class _Handler(BaseHTTPRequestHandler):
    server_version = "localizer-dashboard"
    protocol_version = "HTTP/1.1"

    def __init__(
        self,
        *args,
        collectors: Dict[str, DashboardCollector],
        task_services: Dict[str, TaskService],
        reviews: Dict[str, ReviewService],
        default_variant: str,
        **kwargs,
    ) -> None:
        self._collectors = collectors
        self._task_services = task_services
        self._reviews = reviews
        self._default_variant = default_variant
        self.collector = collectors[default_variant]
        self.tasks = task_services.get(default_variant)
        self.review = reviews.get(default_variant)
        super().__init__(*args, **kwargs)

    # 默认实现会把每条请求打到 stderr，跑起来很吵；保留错误日志即可。
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)
        try:
            self._activate_variant(self._single(params, "variant"))
            self._dispatch(route, params)
        except BrokenPipeError:
            pass
        except ValueError as exc:
            self._json(
                {"error": type(exc).__name__, "message": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
            )
        except Exception as exc:  # 面板不应该因为单个视图异常而整体挂掉
            self._json({"error": type(exc).__name__, "message": str(exc)},
                       status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        if not self._task_services or route not in _POST_ROUTES:
            self._method_not_allowed()
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            self._json(
                {"error": "invalid_content_type"},
                status=HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            )
            return
        if self.headers.get("X-Localizer-Action") != "1":
            self._json({"error": "action_header_required"}, status=HTTPStatus.FORBIDDEN)
            return
        try:
            # 审查提交一次最多 100 条编辑，每条译文可以很长 —— 64 KB 不够。
            limit = _BODY_LIMITS.get(route, 65536)
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > limit:
                raise _PayloadTooLarge(f"request body must be 1-{limit} bytes")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            self._activate_variant(payload.get("variant"))
            if route == "/api/task-profiles":
                result = self.tasks.save_profile(payload)
                self._json(result, status=HTTPStatus.CREATED)
            elif route == "/api/tasks/preflight":
                result = self.tasks.preflight(payload)
                self._json(result)
            elif route == "/api/tasks/confirm-stale":
                result = self.tasks.confirm_stale(payload)
                self._json(
                    result,
                    status=(
                        HTTPStatus.ACCEPTED
                        if result.get("task") is not None
                        else HTTPStatus.OK
                    ),
                )
            elif route == "/api/tasks/run":
                result = self.tasks.submit(payload)
                self._json(result, status=HTTPStatus.ACCEPTED)
            elif route == "/api/tasks/rebuild":
                result = self.tasks.submit_rebuild(payload)
                self._json(result, status=HTTPStatus.ACCEPTED)
            elif route == "/api/tasks/publish":
                result = self.tasks.submit_publish(payload)
                self._json(result, status=HTTPStatus.ACCEPTED)
            else:
                self._review_post(route, payload)
        except _PayloadTooLarge as exc:
            # 超限时我们没有读走请求体。HTTP/1.1 是持久连接，直接回响应再关，
            # 客户端往往还在写 body —— 它撞上 RST，看到的是 ConnectionReset
            # 而不是我们的 413。1 MiB 的体在开发机上大多能挤进 socket 缓冲区，
            # 所以「看着是好的」，换个慢一点的 runner 就间歇性失败。
            # 有界读走一点再关：守规矩的客户端拿得到 413，
            # 攻击者也没法让我们把它想让我们读的字节数全读完。
            self._drain_bounded(limit)
            self.close_connection = True
            self._json(
                {"error": "payload_too_large", "message": str(exc)},
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
        except (ReviewConflict, LogRevisionMismatch) as exc:
            # 冲突不是失败：有运行在跑，或者别人先提交了。重新读一遍再来。
            self._json(
                {"error": type(exc).__name__, "message": str(exc)},
                status=HTTPStatus.CONFLICT,
            )
        except ReviewUnavailable as exc:
            self._json(
                {"error": "review_unavailable", "message": str(exc)},
                status=HTTPStatus.NOT_FOUND,
            )
        except (ValueError, FileNotFoundError, FileExistsError) as exc:
            self._json(
                {"error": type(exc).__name__, "message": str(exc)},
                status=HTTPStatus.BAD_REQUEST,
            )
        except Exception as exc:
            self._json(
                {"error": type(exc).__name__, "message": str(exc)},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _drain_bounded(self, limit: int) -> None:
        """读走并丢弃至多 `limit + _DRAIN_MARGIN` 字节。"""
        try:
            remaining = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            return
        budget = min(max(remaining, 0), limit + _DRAIN_MARGIN)
        while budget > 0:
            chunk = self.rfile.read(min(budget, 65536))
            if not chunk:
                break
            budget -= len(chunk)

    def _method_not_allowed(self) -> None:
        self._json(
            {"error": "method_not_allowed",
             "message": "仅任务预设保存、本地任务启动与 QA 缺陷定点修复支持 POST；"
                        "批量人工翻译与校对仍在 ParaTranz 完成"},
            status=HTTPStatus.METHOD_NOT_ALLOWED,
        )

    def _review_get(self, run_id: str, tail: str, params: dict) -> None:
        service = self.review
        if service is None:
            self._json(
                {"available": False, "reason": "审查视图只在回环地址上启用"},
            )
            return
        view = tail[len("review"):].lstrip("/")
        try:
            if not view or view == "session":
                self._json(service.session(run_id))
            elif view == "glossary":
                self._json(service.glossary_clusters(run_id))
            elif view == "glossary-units":
                self._json(
                    service.glossary_units(
                        run_id, self._single(params, "cluster_id") or ""
                    )
                )
            elif view == "groups":
                majority = self._single(params, "has_majority")
                self._json(
                    service.groups(
                        run_id,
                        limit=self._int(params, "limit", 100),
                        offset=self._int(params, "offset", 0),
                        has_majority=None if majority is None else majority == "1",
                        query=self._single(params, "q") or "",
                    )
                )
            elif view == "units":
                self._json(
                    service.units(
                        run_id,
                        code=self._single(params, "code") or "empty_translation",
                        limit=self._int(params, "limit", 200),
                        offset=self._int(params, "offset", 0),
                    )
                )
            elif view == "unit":
                payload = service.unit(
                    run_id, self._single(params, "stable_identity") or ""
                )
                if payload is None:
                    self._json(
                        {"error": "not_found", "message": "该词条不在本次运行的审查索引里"},
                        status=HTTPStatus.NOT_FOUND,
                    )
                else:
                    self._json(payload)
            elif view == "decisions":
                self._json(
                    service.decisions(run_id, limit=self._int(params, "limit", 100))
                )
            elif view == "recovery":
                self._json(
                    recovery_operations(
                        service,
                        run_id,
                        action=self._single(params, "action") or "unify",
                        limit=self._int(params, "limit", 100),
                    )
                )
            else:
                self._json({"error": "not_found", "message": view},
                           status=HTTPStatus.NOT_FOUND)
        except ReviewUnavailable as exc:
            self._json({"available": False, "reason": str(exc)})

    def _review_post(self, route: str, payload: dict) -> None:
        service = self.review
        if service is None:
            self._method_not_allowed()
            return
        run_id = str(payload.get("run_id", ""))
        revision = payload.get("expected_log_revision")
        if route == "/api/review/recheck":
            self._json(
                service.recheck(
                    run_id,
                    dict(payload.get("edits") or {}),
                    scope=payload.get("scope"),
                )
            )
        elif route == "/api/review/decisions":
            self._json(
                service.mark(
                    run_id,
                    list(payload.get("items") or []),
                    expected_log_revision=revision,
                )
            )
        elif route == "/api/review/commit":
            outcome = service.commit(
                run_id,
                dict(payload.get("edits") or {}),
                reason=str(payload.get("reason", "")),
                expected_log_revision=revision,
                accepted_debt=payload.get("accepted_debt"),
                allow_remote_override=bool(payload.get("allow_remote_override")),
            )
            self._json(outcome.as_dict())
        elif route == "/api/review/unify":
            outcome = service.unify(
                run_id,
                str(payload.get("group_id", "")),
                str(payload.get("translation", "")),
                reason=str(payload.get("reason", "")),
                expected_log_revision=revision,
                allow_remote_override=bool(payload.get("allow_remote_override")),
            )
            self._json(outcome.as_dict())
        elif route == "/api/review/unify-majorities":
            self._json(
                service.unify_majorities(
                    run_id,
                    reason=str(payload.get("reason", "")),
                    expected_log_revision=revision,
                )
            )
        elif route == "/api/review/glossary-exclude":
            self._json(
                service.exclude_glossary_scope(
                    run_id,
                    str(payload.get("cluster_id", "")),
                    str(payload.get("path_glob", "")),
                    reason=str(payload.get("reason", "")),
                    expected_log_revision=revision,
                )
            )
        elif route == "/api/review/revert":
            self._json(
                safe_revert(
                    service,
                    run_id,
                    list(payload.get("decision_ids") or []),
                    reason=str(payload.get("reason", "")),
                    expected_log_revision=revision,
                )
            )
        else:  # pragma: no cover —— 白名单已经挡住
            self._method_not_allowed()

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    do_DELETE = do_PATCH = do_PUT

    # ------------------------------------------------------------------ route

    def _dispatch(self, route: str, params: dict) -> None:
        if route == "/":
            self._static("index.html", "text/html; charset=utf-8")
            return
        if route == "/api/overview":
            self._json(self.collector.overview())
            return
        if route == "/api/runs":
            self._json({"runs": self.collector.list_runs()})
            return
        if route == "/api/task-profiles":
            self._json(
                {
                    "enabled": self.tasks is not None,
                    "profiles": self.tasks.list_profiles() if self.tasks else [],
                }
            )
            return
        if route == "/api/tasks":
            self._json(
                {
                    "enabled": self.tasks is not None,
                    "tasks": self.tasks.list_tasks() if self.tasks else [],
                }
            )
            return
        if route == "/api/tasks/stale-formal":
            if self.tasks is None:
                self._method_not_allowed()
                return
            self._json(
                self.tasks.stale_for_run(self._single(params, "run_id") or "")
            )
            return
        if route.startswith("/api/tasks/"):
            task_id = unquote(route[len("/api/tasks/"):])
            payload = self.tasks.task(task_id) if self.tasks else None
            if payload is None:
                self._json({"error": "not_found", "message": "未知任务"},
                           status=HTTPStatus.NOT_FOUND)
            else:
                self._json(payload)
            return
        if route == "/api/file":
            raw = self._single(params, "path")
            payload = self.collector.read_text_file(unquote(raw)) if raw else None
            if payload is None:
                self._json({"error": "not_found",
                            "message": "文件不存在或不在工作区/输出目录内"},
                           status=HTTPStatus.NOT_FOUND)
                return
            self._json(payload)
            return
        if route.startswith("/api/runs/"):
            rest = route[len("/api/runs/"):]
            run_id, _, tail = rest.partition("/")
            run_id = unquote(run_id)
            if tail.startswith("review"):
                self._review_get(run_id, tail, params)
                return
            if tail == "qa":
                payload = self.collector.qa_issues(
                    run_id,
                    severity=self._single(params, "severity"),
                    code=self._single(params, "code"),
                    query=self._single(params, "q"),
                    limit=self._int(params, "limit", 200),
                    offset=self._int(params, "offset", 0),
                )
            elif not tail:
                payload = self.collector.run_detail(run_id)
            else:
                payload = None
            if payload is None:
                self._json({"error": "not_found", "message": f"未知运行：{run_id}"},
                           status=HTTPStatus.NOT_FOUND)
                return
            self._json(payload)
            return
        self._json({"error": "not_found", "message": route}, status=HTTPStatus.NOT_FOUND)

    def _activate_variant(self, value: object) -> None:
        requested = str(value or self._default_variant).strip()
        if requested not in self._collectors:
            choices = sorted(name for name in self._collectors if name)
            raise ValueError(
                f"unknown variant {requested!r}; available: {choices or ['(single source)']}"
            )
        self.collector = self._collectors[requested]
        self.tasks = self._task_services.get(requested)
        self.review = self._reviews.get(requested)

    @staticmethod
    def _single(params: dict, key: str) -> Optional[str]:
        values = params.get(key)
        return values[0] if values else None

    @staticmethod
    def _int(params: dict, key: str, default: int) -> int:
        raw = params.get(key)
        if not raw:
            return default
        try:
            return int(raw[0])
        except (TypeError, ValueError):
            return default

    # ----------------------------------------------------------------- render

    def _json(self, payload, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", status)

    def _static(self, name: str, content_type: str) -> None:
        path = STATIC_ROOT / name
        if not path.is_file():
            self._json({"error": "missing_asset", "message": name},
                       status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        body = path.read_bytes()
        if name == "index.html":
            # Keep the HTTP response self-contained under the existing inline-only CSP,
            # while allowing the recovery slice to live in a small separately-tested
            # source file instead of making the already large dashboard HTML larger.
            extension = STATIC_ROOT / "review-recovery.js"
            if extension.is_file():
                marker = b"</body>"
                if marker not in body:
                    self._json(
                        {"error": "invalid_asset", "message": "index.html has no </body>"},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                    return
                script = b"\n<script>\n" + extension.read_bytes() + b"\n</script>\n"
                body = body.replace(marker, script + marker, 1)
        self._send(body, content_type)

    def _send(self, body: bytes, content_type: str,
              status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 面板完全自包含，不加载任何外部资源。
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "connect-src 'self'; img-src data:",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.close_connection:
            # 请求体没读完就回了响应（例如 413）。必须显式告诉客户端别复用，
            # 否则残留字节会被当成下一个请求。
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


class DashboardBindError(RuntimeError):
    pass


class DashboardServer:
    def __init__(self, collector: DashboardCollector, host: str = "127.0.0.1",
                 port: int = 8765, enable_tasks: Optional[bool] = None) -> None:
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

        # 不同资源变体共享同一 SQLite TM，因此所有 variant 必须进入同一个
        # 单 worker 队列；否则 RU/PT 页面各自排队仍可能同时写 TM。
        self._task_executor = (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="localizer-task")
            if enable_tasks
            else None
        )
        base_workspace = self._base_workspace(collector)
        shared_profiles = TaskProfileStore(base_workspace) if enable_tasks else None
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

        # 审查与文件读取仍按 variant 隔离；忙碌判据则覆盖所有变体。
        self.reviews = {
            name: ReviewService(
                item.config,
                output_root=item.output,
                workspace_root=item.workspace,
                is_busy=self._tasks_busy,
            )
            for name, item in self.collectors.items()
        } if enable_tasks else {}
        self.review = self.reviews.get(self.default_variant)
        try:
            self._httpd = ThreadingHTTPServer(
                (host, port),
                partial(
                    _Handler,
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
            # Windows 上 10013/10048 很常见：Hyper-V、WSL 或 WinNAT 会成段保留动态端口，
            # 报出来的却是「权限不足」，很容易被误判成要管理员权限。
            raise DashboardBindError(
                f"无法在 {host}:{port} 启动面板：{exc}。\n"
                f"该端口可能被占用或被系统保留。换一个端口：--port 8080，"
                f"或用 --port 0 让系统分配。\n"
                f"Windows 可用 `netsh interface ipv4 show excludedportrange protocol=tcp` "
                f"查看被保留的区间。"
            ) from exc
        self._thread: Optional[threading.Thread] = None

    def _tasks_busy(self) -> bool:
        """有 queued/running 的任务时禁止审查写入。

        TM 是单写者，而且跑到一半的运行会读到半截数据。
        """
        if not self.task_services:
            return False
        return any(
            str(task.get("status")) in {"queued", "running"}
            for service in self.task_services.values()
            for task in service.list_tasks()
        )

    @staticmethod
    def _variant_collectors(
        collector: DashboardCollector,
    ) -> Dict[str, DashboardCollector]:
        variants = collector.config.paths.variants
        if not variants:
            return {"": collector}
        # build_collector 来自真实 project.yaml；重新加载未投影配置，避免在已经
        # 追加了 /live 的 workspace 上再次 for_variant('pts') 变成 /live/pts。
        base = load_project_config(collector.config_path)
        return {
            name: DashboardCollector(
                base.for_variant(name), collector.config_path, collector.repo_root
            )
            for name in sorted(variants)
        }

    @staticmethod
    def _base_workspace(collector: DashboardCollector) -> Path:
        if not collector.config.paths.variants:
            return Path(collector.config.paths.workspace)
        return Path(load_project_config(collector.config_path).paths.workspace)

    @property
    def address(self) -> Tuple[str, int]:
        return self._httpd.server_address[0], self._httpd.server_address[1]

    @property
    def url(self) -> str:
        host, port = self.address
        return f"http://{host}:{port}/"

    def start_background(self) -> "DashboardServer":
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        self._httpd.serve_forever()

    def stop(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._httpd.shutdown()
        self._httpd.server_close()
        for service in self.task_services.values():
            service.shutdown()
        if self._task_executor is not None:
            self._task_executor.shutdown(wait=False, cancel_futures=False)
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> "DashboardServer":
        return self.start_background()

    def __exit__(self, *exc_info) -> None:
        self.stop()


class VariantRequired(ValueError):
    """多变体项目没说清要看哪个变体。"""


def build_collector(
    config_path: Path,
    repo_root: Optional[Path] = None,
    *,
    variant: Optional[str] = None,
) -> DashboardCollector:
    """构造面板收集器。

    多目录项目（只配 `paths.sources`）**必须先投影成单目录配置**，否则有两个
    后果，而且第二个更安静也更糟：

    1. `paths.source` 是 None，`overview()` 在 `_path_status(None)` 直接
       `TypeError: expected str, bytes or os.PathLike object, not NoneType`；
    2. 就算把崩溃挡掉，`paths.workspace` / `paths.output` 指的是**根目录**，
       而 run 实际落在 `<root>/<variant>/` 下 —— 面板会正常打开、然后显示
       「没有运行」。那读起来像「什么都没跑过」，而不是「你在看错的目录」。

    `for_variant()` 已经做了这件事，dashboard 此前从来没调过它。
    """
    config_path = Path(config_path).resolve()
    config = load_project_config(config_path)
    try:
        config = config.for_variant(variant)
    except ValueError as exc:
        raise VariantRequired(
            f"{exc}\n"
            f"面板启动时需要一个初始资源目录：用 `--variant <名字>` 指定，"
            f"或在 project.yaml 里设 `paths.default_variant`；启动后可在页面切换。"
        ) from exc
    root = Path(repo_root).resolve() if repo_root else Path.cwd().resolve()
    return DashboardCollector(config, config_path, root)


def serve(config_path: Path, *, host: str = "127.0.0.1", port: int = 8765,
          repo_root: Optional[Path] = None, variant: Optional[str] = None) -> None:
    server = DashboardServer(
        build_collector(config_path, repo_root, variant=variant), host, port
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
