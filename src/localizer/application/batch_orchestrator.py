from __future__ import annotations

import json
import threading
import time
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from localizer.adapters.providers.openai_compatible import (
    PermanentProviderError,
    ReadTimeoutError,
    TransientProviderError,
)
from localizer.application.prompt import PromptComposer
from localizer.application.token_budget import (
    TokenBudgetBatchPlanner,
    TokenCounter,
    conservative_token_count,
)
from localizer.application.response_parser import (
    NumberingError,
    ResponseParser,
    ResponseProtocolError,
    TruncatedResponse,
)
from localizer.domain.translation_unit import TranslationUnit
from localizer.infrastructure.atomic_io import AtomicIO, AtomicWriteError
from localizer.ports.provider import TranslationProvider
from localizer.rules.validation import QAIssue, ValidationRule
from localizer.rules.placeholder import (
    PlaceholderMap,
    PlaceholderRule,
    has_meaningful_text,
)


BATCH_STATES = {
    "planned",
    "submitted",
    "received",
    "parsed",
    "validated",
    "succeeded",
    "retryable",
    "split_required",
    "failed",
}

# 必须立刻落盘的批次状态：`planned` 决定断点恢复时的批次序号，其余四个是终态
# （恢复要靠它们判断这一批到底完成没有）。submitted/received/parsed/validated
# 是纯观测态，只影响面板显示的即时性，按 min_flush_interval 合并即可。
_DURABLE_BATCH_STATES = {
    "planned",
    "succeeded",
    "retryable",
    "split_required",
    "failed",
}


# Provider 对「请求太大」的报法五花八门，但都是 400/413 系的永久错误。
# 这类错误缩批能解决，其余永久错误缩批只是白烧钱。
_OVERSIZED_MARKERS = (
    "context_length_exceeded",
    "context length",
    "maximum context",
    "too many tokens",
    "reduce the length",
    "request entity too large",
    "payload too large",
    "413",
)


def _is_oversized_request(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _OVERSIZED_MARKERS)


class BudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class UnitResult:
    stable_identity: str
    translation: Optional[str]
    state: str
    issues: Tuple[QAIssue, ...] = ()


@dataclass(frozen=True)
class BatchRunResult:
    results: Tuple[UnitResult, ...]
    requests: int
    input_tokens: int
    output_tokens: int

    @property
    def failed(self) -> Tuple[UnitResult, ...]:
        return tuple(item for item in self.results if item.state == "failed")


class JsonCheckpoint:
    # 每条结果都整文件重写 + fsync + 原子 replace 是 O(n^2)：实测 4001 条时
    # 单次 flush 从 2.7ms 涨到 12.2ms（文件 506 KB），累计写入约 989 MB。
    # 高频的逐条结果按时间合并；批次状态变更这类粗粒度事件仍立即落盘，
    # 保证观测面板看到的进度不会因为合并而滞后一个批次。
    DEFAULT_MIN_FLUSH_INTERVAL = 0.5

    # 连续这么多次落盘失败才认定为真实故障（磁盘满、目录被删、权限全丢）。
    # 单次失败几乎总是并发读句柄造成的瞬时冲突，不该终止运行。
    _MAX_CONSECUTIVE_FLUSH_FAILURES = 12

    def __init__(self, path: Path, *, min_flush_interval: Optional[float] = None) -> None:
        self.path = Path(path).resolve()
        self.min_flush_interval = (
            self.DEFAULT_MIN_FLUSH_INTERVAL
            if min_flush_interval is None
            else max(0.0, float(min_flush_interval))
        )
        self._last_flush = 0.0
        self._pending_flush = False
        # checkpoint 是**进度优化**，不是产物。落盘失败只影响「断点恢复时要重译
        # 几条」，绝不应该有权力终止一轮已经花掉真金白银的翻译。连续失败到
        # _MAX_CONSECUTIVE_FLUSH_FAILURES 才升级为异常 —— 那时说明磁盘满/权限
        # 全丢这类真实故障，继续跑下去也没有意义。
        self._flush_failures = 0
        self._flush_degraded = False
        self._last_flush_error = ""
        self.units: Dict[str, dict] = {}
        # stable_identity -> source_fingerprint，供子运行校验复用安全性。
        self.unit_fingerprints: Dict[str, str] = {}
        self.batches: List[dict] = []
        self.metrics: Dict[str, object] = {
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "translation_units_total": 0,
            "translation_files_total": 0,
            "completed_files": [],
        }
        self.workers: Dict[str, dict] = {}
        self.resources: Dict[str, dict] = {}
        self._unit_resources: Dict[str, str] = {}
        self._lock = threading.RLock()
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping):
                raise ValueError("checkpoint root must be an object")
            self.units = dict(raw.get("units", {}))
            self.unit_fingerprints = dict(raw.get("unit_fingerprints", {}))
            self.batches = list(raw.get("batches", []))
            self.metrics.update(dict(raw.get("metrics", {})))
            self.workers = dict(raw.get("workers", {}))
            self.resources = dict(raw.get("resources", {}))
        self._batch_sequence = sum(
            1 for item in self.batches if item.get("state") == "planned"
        )

    def succeeded(self, identity: str) -> Optional[str]:
        with self._lock:
            item = self.units.get(identity)
            if item and item.get("state") == "succeeded":
                value = item.get("translation")
                return value if isinstance(value, str) else None
            return None

    def batch_summary(self) -> Dict[str, int]:
        """本次运行的批次概览（M6）。

        `metrics` 只有请求数与 token 两个总量，回答不了「这轮为什么贵」——
        2026-08-04 那次 97/98 个失败全部来自**一个** 97 词条批次撞上读超时，
        而 Manifest 里看不出曾经有过缩批、缩到多小、缩了几层。发布说明必须能
        独立回答这件事，否则每次复盘都要去翻 workspace 里的 checkpoint。

        尺寸取 `size`（每条事件都有），`identities` 只是老 checkpoint 的兜底 ——
        它只写在 `planned` 事件上，是写放大的主要来源，随时可能为了进一步削减
        写入量而被去掉。真读 `identities` 的话，那天起「最小批次」会永远是 0，
        缩批就再也看不出来，而且不会有任何报错。
        """
        with self._lock:
            events = list(self.batches)
        summary: Dict[str, int] = {state: 0 for state in BATCH_STATES}
        planned_sizes = []
        for event in events:
            state = event.get("state")
            if state in summary:
                summary[state] += 1
            if state == "planned":
                size = event.get("size")
                if size is None:  # `size` 之前写的 checkpoint
                    size = len(event.get("identities") or ())
                planned_sizes.append(int(size or 0))
        summary["units_planned"] = sum(planned_sizes)
        summary["largest_batch"] = max(planned_sizes) if planned_sizes else 0
        summary["smallest_batch"] = min(planned_sizes) if planned_sizes else 0
        return summary

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def configure_run(
        self,
        *,
        translation_units_total: int,
        translation_files_total: int,
        resource_units: Optional[Mapping[str, Sequence[str]]] = None,
        unit_fingerprints: Optional[Mapping[str, str]] = None,
    ) -> None:
        with self._lock:
            self.metrics["translation_units_total"] = translation_units_total
            self.metrics["translation_files_total"] = translation_files_total
            if unit_fingerprints:
                # 增量重建要靠它**逐条**确认源文没变。没有这层校验，
                # 复用父运行的译文就是把过期内容当成功搬进新运行 ——
                # 而且它不会触发任何 QA 规则（译文本身是合法的），
                # 只是翻的不是现在这句源文。
                #
                # 普通 resume 走的是另一条路：`_process` 直接
                # `checkpoint.succeeded(identity)` 取旧译文，从不看指纹。
                # 两条路因此不对称 —— rebuild 校验、resume 不校验。R15 把可恢复
                # 态放宽到中断态之后这条更要命：被恢复的 run 往往是几小时前被杀
                # 的，中间客户端更新过源文的概率远高于原来「刚刚 failed」的场景。
                #
                # 关键在**比较要发生在覆盖之前**：configure_run 是 update()，
                # 一旦并进去，旧指纹就没了，事后再想比也无从比起。
                self._invalidate_stale_units(unit_fingerprints)
                self.unit_fingerprints.update(dict(unit_fingerprints))
            if resource_units is not None:
                now = self._now()
                for resource_path, identities in resource_units.items():
                    identity_list = list(identities)
                    for identity in identity_list:
                        self._unit_resources[identity] = resource_path
                    states = Counter(
                        str((self.units.get(identity) or {}).get("state", "pending"))
                        for identity in identity_list
                    )
                    previous = dict(self.resources.get(resource_path, {}))
                    self.resources[resource_path] = {
                        "state": previous.get("state", "queued"),
                        "worker_id": previous.get("worker_id"),
                        "units_total": len(identity_list),
                        "units_succeeded": states.get("succeeded", 0),
                        "units_failed": states.get("failed", 0),
                        "batches_total": int(previous.get("batches_total") or 0),
                        "requests": int(previous.get("requests") or 0),
                        "input_tokens": int(previous.get("input_tokens") or 0),
                        "output_tokens": int(previous.get("output_tokens") or 0),
                        "batch_id": previous.get("batch_id"),
                        "batch_state": previous.get("batch_state"),
                        "reason": previous.get("reason", ""),
                        "started_at": previous.get("started_at"),
                        "completed_at": previous.get("completed_at"),
                        "updated_at": now,
                    }
            self._flush(force=True)

    def start_resource(
        self,
        worker_id: str,
        resource_path: str,
        identities: Sequence[str],
    ) -> None:
        with self._lock:
            now = self._now()
            resource = self.resources.setdefault(
                resource_path,
                {
                    "units_total": len(identities),
                    "units_succeeded": 0,
                    "units_failed": 0,
                    "batches_total": 0,
                    "requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                },
            )
            for identity in identities:
                self._unit_resources[identity] = resource_path
            resource.update(
                {
                    "state": "running",
                    "worker_id": worker_id,
                    "batch_id": None,
                    "batch_state": None,
                    "reason": "",
                    "started_at": resource.get("started_at") or now,
                    "completed_at": None,
                    "updated_at": now,
                }
            )
            self.workers[worker_id] = {
                "state": "running",
                "resource_path": resource_path,
                "batch_id": None,
                "batch_size": 0,
                "batch_state": None,
                "updated_at": now,
            }
            self._flush(force=True)

    def start_batch(
        self,
        identities: Sequence[str],
        *,
        resource_path: str,
        worker_id: str,
    ) -> str:
        with self._lock:
            self._batch_sequence += 1
            batch_id = f"batch-{self._batch_sequence:06d}"
        self.record_batch(
            identities,
            "planned",
            batch_id=batch_id,
            resource_path=resource_path,
            worker_id=worker_id,
        )
        return batch_id

    def record_batch(
        self,
        identities: Sequence[str],
        state: str,
        reason: str = "",
        *,
        batch_id: Optional[str] = None,
        resource_path: str = "",
        worker_id: str = "translation-0",
        request_delta: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        if state not in BATCH_STATES:
            raise ValueError(f"unknown batch state: {state}")
        with self._lock:
            event = {
                "batch_id": batch_id,
                # identities 只在 `planned` 事件上写一次。一个批次会产生 6 条事件，
                # 每条都重复整份 identities 是写放大的**主要来源**：实测 10 文件下
                # 1000/2000/4000 词条累计写入 72.8 / 256.4 / 958.3 MB，比值 3.52 与
                # 3.74（2.0=线性，4.0=二次），外推全量冷启动是几百 GB。
                # 消费方只用 len(identities)，所以其余事件带一个 size 就够。
                "size": len(identities),
                "resource_path": resource_path,
                "worker_id": worker_id,
                "state": state,
                "reason": reason,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "timestamp": self._now(),
            }
            if state == "planned":
                event["identities"] = list(identities)
            self.batches.append(event)
            self.metrics["requests"] = int(
                self.metrics.get("requests", 0)) + request_delta
            self.metrics["input_tokens"] = int(
                self.metrics.get("input_tokens", 0)
            ) + input_tokens
            self.metrics["output_tokens"] = int(
                self.metrics.get("output_tokens", 0)
            ) + output_tokens
            self.workers[worker_id] = {
                "state": "running",
                "resource_path": resource_path,
                "batch_id": batch_id,
                "batch_size": len(identities),
                "batch_state": state,
                "updated_at": event["timestamp"],
            }
            if resource_path:
                resource = self.resources.setdefault(resource_path, {})
                resource.update(
                    {
                        "state": "running",
                        "worker_id": worker_id,
                        "batch_id": batch_id,
                        "batch_state": state,
                        "reason": reason,
                        "updated_at": event["timestamp"],
                    }
                )
                if state == "planned":
                    resource["batches_total"] = int(
                        resource.get("batches_total") or 0
                    ) + 1
                if state == "submitted":
                    resource["requests"] = int(resource.get("requests") or 0) + 1
                resource["input_tokens"] = int(
                    resource.get("input_tokens") or 0
                ) + input_tokens
                resource["output_tokens"] = int(
                    resource.get("output_tokens") or 0
                ) + output_tokens
            # 只有批次的**终态**值得立刻落盘。submitted/received/parsed/validated
            # 是纯观测态，断点恢复只依赖 units 与批次终态，它们丢了不影响正确性，
            # 却贡献了 96 文件规模下 769 次 flush 里的大部分。
            self._flush(force=state in _DURABLE_BATCH_STATES)

    def set_worker(
        self,
        worker_id: str,
        *,
        state: str,
        resource_path: str = "",
        batch_id: Optional[str] = None,
        batch_size: int = 0,
    ) -> None:
        with self._lock:
            self.workers[worker_id] = {
                "state": state,
                "resource_path": resource_path,
                "batch_id": batch_id,
                "batch_size": batch_size,
                "batch_state": None,
                "updated_at": self._now(),
            }
            self._flush()

    def complete_resource(self, worker_id: str, resource_path: str) -> None:
        with self._lock:
            now = self._now()
            completed = set(self.metrics.get("completed_files", []))
            completed.add(resource_path)
            self.metrics["completed_files"] = sorted(completed)
            resource = self.resources.setdefault(resource_path, {})
            resource.update(
                {
                    "state": (
                        "completed_with_failures"
                        if int(resource.get("units_failed") or 0)
                        else "completed"
                    ),
                    "worker_id": worker_id,
                    "batch_id": None,
                    "batch_state": None,
                    "completed_at": now,
                    "updated_at": now,
                }
            )
            self.workers[worker_id] = {
                "state": "idle",
                "resource_path": resource_path,
                "batch_id": None,
                "batch_size": 0,
                "batch_state": None,
                "updated_at": now,
            }
            self._flush(force=True)

    def fail_resource(self, worker_id: str, resource_path: str, reason: str) -> None:
        with self._lock:
            now = self._now()
            resource = self.resources.setdefault(resource_path, {})
            resource.update(
                {
                    "state": "failed",
                    "worker_id": worker_id,
                    "batch_state": "failed",
                    "reason": reason,
                    "completed_at": now,
                    "updated_at": now,
                }
            )
            self.workers[worker_id] = {
                "state": "failed",
                "resource_path": resource_path,
                "batch_id": resource.get("batch_id"),
                "batch_size": 0,
                "batch_state": "failed",
                "reason": reason,
                "updated_at": now,
            }
            # 这里是**异常路径**：调用方刚捕获了一个业务首因，正要把它上抛。
            # 落盘再抛第二个异常会把首因完全吞掉（`raise` 语句根本执行不到），
            # 用户只看到「存取被拒」，真正的原因永远查不到。
            try:
                self._flush(force=True)
            except AtomicWriteError:
                pass

    def _invalidate_stale_units(self, fingerprints: Mapping[str, str]) -> None:
        """源文指纹变了的坐标，把它缓存的成功译文降回待译。

        调用方已持锁。返回前不 flush —— configure_run 末尾统一落盘。

        译文本身完全合法，任何 QA 规则都发现不了：它只是翻的不是现在这句源文。
        所以这里必须主动作废，不能指望下游拦。
        """
        stale = []
        for identity, fingerprint in fingerprints.items():
            previous = self.unit_fingerprints.get(identity)
            if previous is None or previous == fingerprint:
                continue
            item = self.units.get(identity)
            if not item or item.get("state") != "succeeded":
                continue
            self.units[identity] = {
                "state": "stale_source",
                "translation": None,
                "issues": [],
            }
            stale.append(identity)
        if stale:
            # 计入 metrics 而不是只打日志：一次恢复作废了多少条已付费译文，
            # 是运维判断「这次恢复到底省了多少」的唯一依据。
            self.metrics["stale_source_invalidated"] = int(
                self.metrics.get("stale_source_invalidated", 0)
            ) + len(stale)

    def record_result(self, result: UnitResult) -> None:
        with self._lock:
            previous = self.units.get(result.stable_identity) or {}
            self.units[result.stable_identity] = {
                "state": result.state,
                "translation": result.translation,
                "issues": [asdict(issue) for issue in result.issues],
            }
            resource_path = self._unit_resources.get(result.stable_identity)
            if resource_path:
                resource = self.resources.setdefault(resource_path, {})
                for state, field in (
                    ("succeeded", "units_succeeded"),
                    ("failed", "units_failed"),
                ):
                    if previous.get("state") == state:
                        resource[field] = max(0, int(resource.get(field) or 0) - 1)
                    if result.state == state:
                        resource[field] = int(resource.get(field) or 0) + 1
                resource["updated_at"] = self._now()
            self._flush()

    def _flush(self, *, force: bool = False) -> None:
        """把状态落盘。force=False 时按 min_flush_interval 合并高频写入。

        丢失窗口内的结果只意味着恢复时重译那几条，代价远小于把整轮运行
        的 I/O 放大成几百 MB。

        落盘失败**不上抛**：checkpoint 只是进度优化。真机上曾经发生过完整链路
        —— 面板轮询持有读句柄 → 写侧 replace 撞 WinError 5 → `record_batch` 抛出
        → `run()` 的 except 调 `fail_resource` → `fail_resource` 内部的 `_flush`
        **再抛一次把首因彻底吞掉** → 4 个 worker 连带整轮 preview 中止，用户在
        面板上只看到一句「存取被拒」。现在改为标记降级、保留 `_pending_flush`，
        由下一次 flush 重试；只有连续失败到阈值才升级为异常。
        """
        now = time.monotonic()
        if not force and (now - self._last_flush) < self.min_flush_interval:
            self._pending_flush = True
            return
        try:
            AtomicIO.write_json(
                self.path,
                {
                    "schema_version": 3,
                    "units": self.units,
                    "unit_fingerprints": self.unit_fingerprints,
                    "batches": self.batches,
                    "metrics": self.metrics,
                    "workers": self.workers,
                    "resources": self.resources,
                },
            )
        except AtomicWriteError as exc:
            self._flush_failures += 1
            self._flush_degraded = True
            self._last_flush_error = str(exc)
            # 状态没落盘 —— 必须保持 pending，否则下一次合并窗口会以为
            # 「已经写过了」而永远不补。
            self._pending_flush = True
            self.metrics["checkpoint_degraded"] = True
            self.metrics["checkpoint_flush_failures"] = self._flush_failures
            self.metrics["checkpoint_last_error"] = self._last_flush_error
            if self._flush_failures >= self._MAX_CONSECUTIVE_FLUSH_FAILURES:
                raise
            return
        self._last_flush = now
        self._pending_flush = False
        self._flush_failures = 0
        if self._flush_degraded:
            # 恢复了也要留痕：面板不能显示「一切正常」而抹掉中间那段窗口。
            self._flush_degraded = False
            self.metrics["checkpoint_degraded"] = False
            self.metrics["checkpoint_recovered_after_failures"] = True

    def flush_now(self) -> None:
        """强制落盘一次（外部调用点，例如批次收尾）。"""
        with self._lock:
            self._flush(force=True)

    def finalize(self) -> None:
        """运行收尾：把合并窗口里还没落盘的状态强制写出。

        **必须放在 try/finally 里调用**：异常路径下窗口里那几条已经付过钱的
        译文同样需要落盘，否则断点恢复会重新买一次。这里对失败保持宽容 ——
        收尾时再抛异常只会掩盖真正让运行失败的那个首因。
        """
        with self._lock:
            if not self._pending_flush:
                return
            try:
                self._flush(force=True)
            except AtomicWriteError:
                # 已经记进 metrics 与降级标记；收尾不制造第二个异常。
                pass


Validator = Callable[[TranslationUnit, str], Tuple[QAIssue, ...]]


class BatchOrchestrator:
    def __init__(
        self,
        provider: TranslationProvider,
        composer: PromptComposer,
        checkpoint: JsonCheckpoint,
        *,
        validator: Optional[Validator] = None,
        parser: Optional[ResponseParser] = None,
        validation_rule: Optional[ValidationRule] = None,
        max_transient_retries: int = 2,
        max_requests: int = 128,
        # 读超时缩批的深度上限。97 条二分到 1 条要 7 层；给 8 层留余量，
        # 同时最坏情况的请求总数仍受 max_requests 约束。
        max_timeout_splits: int = 8,
        max_total_tokens: int = 1_000_000,
        context_window: int = 32_000,
        max_output_tokens: int = 4_096,
        token_counter: TokenCounter = conservative_token_count,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.provider = provider
        self.composer = composer
        self.checkpoint = checkpoint
        self.parser = parser or ResponseParser()
        self.validator = validator
        # 必须由调用方注入项目的 rules.yaml。此前这里硬编码 ValidationRule()，
        # 于是一次运行存在两套规则：翻译阶段用空规则、构建阶段用项目规则。
        # 后果是启用 G04 白名单后，命中白名单的**正确**译文在翻译阶段被判
        # source_language_residue → failed，release 被永久阻断，错误文案还写着
        # "not allowed by rules.yaml"（而 rules.yaml 明确允许它）。
        self.validation_rule = validation_rule or ValidationRule()
        # 占位符预设按 adapter_id 取：源文与译文必须用同一套，
        # 否则多重集比对必然失配。generic 那套只覆盖 printf 与 {name}，
        # 对 $VAR$ / £icon£ / §Y / [Root.GetName] 全部零匹配且静默通过。
        self._placeholder_rules: Dict[str, PlaceholderRule] = {}
        self.placeholder_maps: Dict[str, PlaceholderMap] = {}
        # stable_identity -> 掩码后仍「像占位符」的片段，说明有语法没被覆盖。
        self.unknown_placeholders: Dict[str, Tuple[str, ...]] = {}
        self.max_transient_retries = max_transient_retries
        self.max_requests = max_requests
        if max_timeout_splits < 0:
            raise ValueError("max_timeout_splits must not be negative")
        self.max_timeout_splits = max_timeout_splits
        self.max_total_tokens = max_total_tokens
        self.batch_planner = TokenBudgetBatchPlanner(
            composer,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            count_tokens=token_counter,
        )
        self.sleep = sleep
        self.requests = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def _placeholder_rule_for(self, adapter_id: str) -> PlaceholderRule:
        rule = self._placeholder_rules.get(adapter_id)
        if rule is None:
            rule = PlaceholderRule.for_adapter(adapter_id)
            self._placeholder_rules[adapter_id] = rule
        return rule

    def run(
        self,
        units: Sequence[TranslationUnit],
        *,
        resource_path: Optional[str] = None,
        worker_id: str = "translation-0",
    ) -> BatchRunResult:
        active_path = resource_path or self._resource_label(units)
        self._resource_path = active_path
        self._worker_id = worker_id
        self.checkpoint.start_resource(
            worker_id,
            active_path,
            [unit.stable_identity for unit in units],
        )
        results: Dict[str, UnitResult] = {}
        pending = []
        resource_failed = False
        for unit in units:
            existing = self.checkpoint.succeeded(unit.stable_identity)
            if existing is not None:
                results[unit.stable_identity] = UnitResult(
                    unit.stable_identity, existing, "succeeded"
                )
            else:
                placeholder_map = self._placeholder_rule_for(unit.adapter_id).mask(
                    unit.source_text, namespace=unit.stable_identity
                )
                self.placeholder_maps[unit.stable_identity] = placeholder_map
                unknown = self._placeholder_rule_for(
                    unit.adapter_id
                ).find_unmasked_candidates(placeholder_map.masked_text)
                if unknown:
                    self.unknown_placeholders[unit.stable_identity] = unknown
                pending.append(
                    replace(
                        unit,
                        source_text=placeholder_map.masked_text,
                        placeholders=placeholder_map.tokens,
                    )
                )
        try:
            for batch in self.batch_planner.plan(pending):
                estimate = self.batch_planner.estimate(batch)
                if not estimate.fits:
                    reason = (
                        "single unit exceeds provider token budget: "
                        f"input={estimate.input_tokens}/{estimate.input_budget}, "
                        f"estimated_output={estimate.output_tokens}/{estimate.output_budget}"
                    )
                    for result in self._fail_all(batch, reason):
                        results[result.stable_identity] = result
                    continue
                for result in self._process(batch, qa_retry=False):
                    results[result.stable_identity] = result
        except BudgetExceeded as exc:
            issue = QAIssue("budget_exceeded", "error", str(exc), {})
            for unit in pending:
                if unit.stable_identity in results:
                    continue
                existing = self.checkpoint.succeeded(unit.stable_identity)
                if existing is not None:
                    results[unit.stable_identity] = UnitResult(
                        unit.stable_identity, existing, "succeeded"
                    )
                else:
                    failed = UnitResult(
                        unit.stable_identity, None, "failed", (issue,))
                    self.checkpoint.record_result(failed)
                    results[unit.stable_identity] = failed
        except Exception as exc:
            resource_failed = True
            self.checkpoint.fail_resource(worker_id, active_path, str(exc))
            raise
        finally:
            if not resource_failed:
                self.checkpoint.complete_resource(worker_id, active_path)
        ordered = tuple(results[unit.stable_identity] for unit in units)
        return BatchRunResult(
            ordered, self.requests, self.input_tokens, self.output_tokens
        )

    @staticmethod
    def _resource_label(units: Sequence[TranslationUnit]) -> str:
        paths = sorted({unit.relative_path for unit in units})
        if not paths:
            return ""
        return paths[0] if len(paths) == 1 else "<multiple resources>"

    def _process(
        self,
        units: Tuple[TranslationUnit, ...],
        *,
        qa_retry: bool,
        timeout_splits_left: Optional[int] = None,
    ) -> Iterable[UnitResult]:
        """翻译一个批次。

        `timeout_splits_left` 是读超时缩批的**全局预算**，随递归向下传递而**不重置**。
        这是这次改动的要害：原来 `_fail_or_split` 递归进 `_process` 会把
        `transient_attempt` 归零、退避随之重置，实测持续 429 下 16 条批次打出
        93 次请求（16,16,16,8,8,8,4,4,4,2,2,2,1,1,1…）。所以缩批本身不是问题，
        **缩批顺带把重试预算也重置了**才是。这个预算只减不增。
        """
        if not units:
            return ()
        if timeout_splits_left is None:
            timeout_splits_left = self.max_timeout_splits
        identities = [unit.stable_identity for unit in units]
        batch_id = self.checkpoint.start_batch(
            identities,
            resource_path=self._resource_path,
            worker_id=self._worker_id,
        )
        protocol_repair_used = False
        transient_attempt = 0
        while True:
            repair = protocol_repair_used
            try:
                self._check_request_budget()
                self.checkpoint.record_batch(
                    identities,
                    "submitted",
                    batch_id=batch_id,
                    resource_path=self._resource_path,
                    worker_id=self._worker_id,
                    request_delta=1,
                )
                response = self.provider.translate(
                    self.composer.compose(units, repair=repair), units
                )
                self.requests += 1
                self.input_tokens += response.usage.input_tokens
                self.output_tokens += response.usage.output_tokens
                # Provider 已经返回即产生实际消耗。先落盘 usage，再解析协议；
                # 即便后续因编号、截断等问题重试，WebUI 和预算也不能漏记 Token。
                self.checkpoint.record_batch(
                    identities,
                    "received",
                    batch_id=batch_id,
                    resource_path=self._resource_path,
                    worker_id=self._worker_id,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                )
                self._check_token_budget()
                parsed = self.parser.parse(response, len(units))
                self.checkpoint.record_batch(
                    identities,
                    "parsed",
                    batch_id=batch_id,
                    resource_path=self._resource_path,
                    worker_id=self._worker_id,
                )
                break
            except TransientProviderError as exc:
                self.requests += 1
                transient_attempt += 1
                if transient_attempt > self.max_transient_retries:
                    # 429 / 5xx / 建连失败：端点整体不可用或在限流。缩批只会在同一个
                    # 端点上打出更多请求 —— 实测持续 429 下 16 条批次打出 93 次请求
                    # （16,16,16,8,8,8,4,4,4,2,2,2,1,1,1…），93 秒内连打 93 次，
                    # 最终 16 条仍全部失败。这类一律整批判死，不缩批。
                    #
                    # **读超时是另一回事**：请求已经发出去了，是这一次请求本身太重。
                    # 2026-08-04 真机 preview 的 98 个失败里有 97 个来自单独一个
                    # 97 条批次连续三次撞 120 秒读超时 —— 那 97 条本身没有任何问题。
                    if isinstance(exc, ReadTimeoutError) and len(units) > 1:
                        if timeout_splits_left <= 0:
                            return self._fail_all(
                                units,
                                f"read timeout and bisection budget exhausted: {exc}",
                                batch_id=batch_id,
                            )
                        self.checkpoint.record_batch(
                            identities,
                            "split_required",
                            f"read timeout on {len(units)} units: {exc}",
                            batch_id=batch_id,
                            resource_path=self._resource_path,
                            worker_id=self._worker_id,
                        )
                        # 预算不重置地传下去 —— 这正是旧实现的病根。
                        return self._split_or_fail(
                            units,
                            str(exc),
                            qa_retry=qa_retry,
                            parent_batch_id=batch_id,
                            timeout_splits_left=timeout_splits_left - 1,
                        )
                    return self._fail_all(
                        units,
                        f"provider transient retries exhausted: {exc}",
                        batch_id=batch_id,
                    )
                self.checkpoint.record_batch(
                    identities,
                    "retryable",
                    str(exc),
                    batch_id=batch_id,
                    resource_path=self._resource_path,
                    worker_id=self._worker_id,
                )
                self.sleep(float(2 ** (transient_attempt - 1)))
            except TruncatedResponse as exc:
                self.checkpoint.record_batch(
                    identities,
                    "split_required",
                    str(exc),
                    batch_id=batch_id,
                    resource_path=self._resource_path,
                    worker_id=self._worker_id,
                )
                return self._split_or_fail(
                    units, str(exc), qa_retry=qa_retry, parent_batch_id=batch_id
                )
            except (NumberingError, ResponseProtocolError) as exc:
                if not protocol_repair_used:
                    protocol_repair_used = True
                    self.checkpoint.record_batch(
                        identities,
                        "retryable",
                        str(exc),
                        batch_id=batch_id,
                        resource_path=self._resource_path,
                        worker_id=self._worker_id,
                    )
                    continue
                self.checkpoint.record_batch(
                    identities,
                    "split_required",
                    str(exc),
                    batch_id=batch_id,
                    resource_path=self._resource_path,
                    worker_id=self._worker_id,
                )
                return self._split_or_fail(
                    units, str(exc), qa_retry=qa_retry, parent_batch_id=batch_id
                )
            except PermanentProviderError as exc:
                # 请求确实发出去了，必须计入预算。原实现这个分支不 +1，于是一轮
                # 全失败的运行 result.requests == 0，看起来像「一次都没调用」。
                self.requests += 1
                if _is_oversized_request(exc):
                    # 上下文超限是**尺寸问题**，缩批正好能解决；原实现一次性
                    # _fail_all，整批判死且不做任何缩批尝试，用户只看到一个
                    # HTTP 400。这是缩批真正该发挥作用的场合。
                    return self._fail_or_split(
                        units,
                        f"oversized request: {exc}",
                        qa_retry=qa_retry,
                        batch_id=batch_id,
                    )
                return self._fail_all(units, str(exc), batch_id=batch_id)

        successes: List[UnitResult] = []
        failed_units: List[TranslationUnit] = []
        failed_issues: Dict[str, Tuple[QAIssue, ...]] = {}
        for unit, translation in zip(units, parsed.translations):
            normalized, issues = self._validate_and_restore(unit, translation)
            if any(issue.severity == "error" for issue in issues):
                failed_units.append(unit)
                failed_issues[unit.stable_identity] = issues
                continue
            result = UnitResult(unit.stable_identity,
                                normalized, "succeeded", issues)
            self.checkpoint.record_result(result)
            successes.append(result)
        self.checkpoint.record_batch(
            identities,
            "validated",
            batch_id=batch_id,
            resource_path=self._resource_path,
            worker_id=self._worker_id,
        )
        if not failed_units:
            self.checkpoint.record_batch(
                identities,
                "succeeded",
                batch_id=batch_id,
                resource_path=self._resource_path,
                worker_id=self._worker_id,
            )
            return tuple(successes)
        # Content QA retries only the failing units. Successful siblings were checkpointed above.
        if qa_retry:
            retried = []
            for unit in failed_units:
                result = UnitResult(
                    unit.stable_identity,
                    None,
                    "failed",
                    failed_issues[unit.stable_identity],
                )
                self.checkpoint.record_result(result)
                retried.append(result)
            self.checkpoint.record_batch(
                identities,
                "failed",
                "content QA retry failed",
                batch_id=batch_id,
                resource_path=self._resource_path,
                worker_id=self._worker_id,
            )
            return tuple(successes + retried)
        self.checkpoint.record_batch(
            identities,
            "split_required",
            f"content QA retry for {len(failed_units)} unit(s)",
            batch_id=batch_id,
            resource_path=self._resource_path,
            worker_id=self._worker_id,
        )
        retried = list(self._process(tuple(failed_units), qa_retry=True))
        retry_by_id = {item.stable_identity: item for item in retried}
        for unit in failed_units:
            if unit.stable_identity not in retry_by_id:
                result = UnitResult(
                    unit.stable_identity,
                    None,
                    "failed",
                    failed_issues[unit.stable_identity],
                )
                self.checkpoint.record_result(result)
                retried.append(result)
        return tuple(successes + retried)

    def _fail_or_split(
        self,
        units: Tuple[TranslationUnit, ...],
        reason: str,
        *,
        qa_retry: bool,
        batch_id: Optional[str] = None,
        timeout_splits_left: Optional[int] = None,
    ) -> Tuple[UnitResult, ...]:
        if len(units) > 1:
            return self._split_or_fail(
                units,
                reason,
                qa_retry=qa_retry,
                parent_batch_id=batch_id,
                timeout_splits_left=timeout_splits_left,
            )
        return self._fail_all(units, reason, batch_id=batch_id)

    def _split_or_fail(
        self,
        units: Tuple[TranslationUnit, ...],
        reason: str,
        *,
        qa_retry: bool,
        parent_batch_id: Optional[str] = None,
        timeout_splits_left: Optional[int] = None,
    ) -> Tuple[UnitResult, ...]:
        if len(units) == 1:
            return self._fail_all(units, reason, batch_id=parent_batch_id)
        midpoint = len(units) // 2
        return tuple(
            self._process(
                units[:midpoint],
                qa_retry=qa_retry,
                timeout_splits_left=timeout_splits_left,
            )
        ) + tuple(
            self._process(
                units[midpoint:],
                qa_retry=qa_retry,
                timeout_splits_left=timeout_splits_left,
            )
        )

    def _fail_all(
        self,
        units: Sequence[TranslationUnit],
        reason: str,
        *,
        batch_id: Optional[str] = None,
    ) -> Tuple[UnitResult, ...]:
        issue = QAIssue("translation_failed", "error", reason, {})
        results = []
        for unit in units:
            result = UnitResult(unit.stable_identity, None, "failed", (issue,))
            self.checkpoint.record_result(result)
            results.append(result)
        if batch_id is None:
            batch_id = self.checkpoint.start_batch(
                [unit.stable_identity for unit in units],
                resource_path=self._resource_path,
                worker_id=self._worker_id,
            )
        self.checkpoint.record_batch(
            [unit.stable_identity for unit in units],
            "failed",
            reason,
            batch_id=batch_id,
            resource_path=self._resource_path,
            worker_id=self._worker_id,
        )
        return tuple(results)

    def _check_request_budget(self) -> None:
        if self.requests >= self.max_requests:
            raise BudgetExceeded(f"request limit reached: {self.max_requests}")

    def _check_token_budget(self) -> None:
        total = self.input_tokens + self.output_tokens
        if total > self.max_total_tokens:
            raise BudgetExceeded(
                f"token limit exceeded: {total} > {self.max_total_tokens}")

    def _validate_and_restore(
        self, unit: TranslationUnit, translation: str
    ) -> Tuple[str, Tuple[QAIssue, ...]]:
        placeholder_map = self.placeholder_maps[unit.stable_identity]
        placeholder_rule = self._placeholder_rule_for(unit.adapter_id)
        issues = list(
            self.validation_rule.validate_masked_translation(
                translation, placeholder_map
            )
        )
        normalized = translation
        if not any(issue.code == "placeholder_mismatch" for issue in issues):
            normalized = placeholder_rule.restore(translation, placeholder_map)
        summary = self.validation_rule.validate_text(
            normalized,
            adapter_id=unit.adapter_id,
            relative_path=unit.relative_path,
        )
        issues.extend(summary.issues)
        unknown = self.unknown_placeholders.get(unit.stable_identity, ())
        if unknown:
            # warning 而非 error：探测式必然宽于真实语法，用它阻断发布会误伤。
            # 它的价值是让「有语法没被识别」这件事在 QA 报告里可见，
            # 而不是像现在这样零匹配 + round_trip 恒真地静默通过。
            issues.append(
                QAIssue(
                    "unknown_placeholder",
                    "warning",
                    "source contains placeholder-like syntax no rule recognises",
                    {"fragments": list(unknown)},
                )
            )
        original_source = placeholder_rule.restore(
            unit.source_text, placeholder_map)
        # 纯占位符/纯符号条目（$VALUE$、纯数字、纯人名）本来就不该被翻译，
        # 译文与源文必然相同。任何键值型格式都有这类条目，判 untranslated
        # 会大批误报并直接阻断 release。
        # unit.source_text 在批次里已经是掩码后的形态，直接用掩码版判据。
        if not has_meaningful_text(unit.source_text):
            pass
        elif summary.text.strip() == original_source.strip():
            issues.append(
                QAIssue(
                    "untranslated",
                    "error",
                    "translation is identical to source text",
                    {},
                )
            )
        # restore 之后再扫一遍残留的占位符 token 变体。
        # find_tokens 的多重集比较只认严格半角小写形式：模型把占位符写成
        # 【PH_xxx_0】同时又保留一份半角的，两边计数仍然 1:1 相等，
        # restore 也不认识全角那份，字面量就这样直达 .mo。
        residue = placeholder_rule.find_token_residue(summary.text)
        if residue:
            issues.append(
                QAIssue(
                    "placeholder_variant_residue",
                    "error",
                    "placeholder token survived restore in a variant form",
                    {"fragments": list(residue)},
                )
            )
        if self.validator is not None:
            issues.extend(self.validator(unit, summary.text))
        return summary.text, tuple(issues)
