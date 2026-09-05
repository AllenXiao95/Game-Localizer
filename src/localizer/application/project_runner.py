from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from queue import Empty, Queue
from typing import Dict, Mapping, Optional, Sequence, Tuple

from localizer.adapters.providers.openai_compatible import (
    OpenAICompatibleProvider,
    OpenAICompatibleSettings,
)
from localizer.adapters.resources import build_adapter
from localizer.adapters.storage.sqlite_tm import SQLiteTranslationMemory, TMEntry
from localizer.adapters.storage.glossary import GlossaryRepository
from localizer.application.batch_orchestrator import (
    BatchOrchestrator,
    JsonCheckpoint,
    UnitResult,
)
from localizer.application.local_build import (
    BuildMode,
    LocalBuildPipeline,
    LocalBuildResult,
    ResourceBuild,
    resolve_run_output,
)
from localizer.application.prompt import PromptComposer
from localizer.application.quality_gate import (
    PROVENANCE_THIS_RUN,
    LegacyDebtBaseline,
    QualityGate,
)
from localizer.application.scan import ResourceScanner
from localizer.application.translation_evidence import (
    TranslationEvidenceStore,
    aggregate_execution_metrics,
    normalize_execution_record,
)
from localizer.application.translation_plan import TranslationPlan, TranslationPlanner
from localizer.config.models import ProjectConfig
from localizer.infrastructure.atomic_io import AtomicIO
from localizer.infrastructure.token_counting import (
    build_token_counter,
    warm_up_token_counter,
)
from localizer.infrastructure.workspace import validate_run_id
from localizer.ports.provider import TranslationProvider
from localizer.rules.loader import load_filter_rules, load_validation_rule


class StaleFormalEntryError(RuntimeError):
    """TM 里已是 formal 的坐标，源文却已经变了 —— 本次翻译结果写不进去。

    这是既有地雷，不只影响审查面板：只要某个坐标被上一轮 release 晋升成 formal、
    之后源文又被改动，下一轮的机器重译就会被 upsert 的 WHERE 静默挡下，
    然后一直跑到 render 之后才在 `validate_promotable_run` 炸出来。
    """

    def __init__(self, message: str, identities: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.identities = tuple(str(value) for value in identities if value)


@dataclass(frozen=True)
class RebuildPlan:
    """父运行的复用计划。生成它不需要任何 Provider 调用。"""

    parent_run_id: str
    # 真正提供可复用机器结果的 checkpoint。通常等于 parent_run_id；如果选中的
    # 最新运行没有调用 Provider，则沿运行谱系回溯到最近的物化 checkpoint。
    reuse_checkpoint_run_id: str
    # 从 reuse checkpoint 直接复用的译文。
    reused: Mapping[str, str]
    # 仍然要送模型的坐标。
    retry: Tuple[str, ...]
    # 父运行成功、但源文已经变了 —— 不能复用，必须重译。
    stale: Tuple[str, ...]
    # 已经由人工定稿、这次连模型都不用调的坐标。
    resolved_by_human: Tuple[str, ...]

    def as_dict(self) -> dict:
        return {
            "parent_run_id": self.parent_run_id,
            "reuse_checkpoint_run_id": self.reuse_checkpoint_run_id,
            "reused": len(self.reused),
            "retried": len(self.retry),
            "stale": len(self.stale),
            "resolved_by_human": len(self.resolved_by_human),
        }


class IncompatibleParentRun(RuntimeError):
    """父运行与当前配置不兼容，复用它的译文不安全。"""


@dataclass(frozen=True)
class ProjectRunResult:
    build: LocalBuildResult
    extracted_units: int
    tm_hits: int
    machine_successes: int
    failed_units: int
    rebuild: Optional[RebuildPlan] = None


@dataclass(frozen=True)
class ResourceQueueItem:
    """现有 resource worker queue 的轻量排序视图。

    `group` 始终保留完整执行输入；`estimated_work` 只用于 claim 顺序，绝不参与
    结果索引或 BatchOrchestrator 的 resume 语义。
    """

    original_index: int
    group: Tuple[TranslationUnit, ...]
    relative_path: str
    estimated_work: int
    remaining_units: int
    estimate_kind: str


class ProjectRunner:
    def __init__(
        self,
        config: ProjectConfig,
        *,
        provider: Optional[TranslationProvider] = None,
    ) -> None:
        self.config = config
        self._token_counter = None
        self.provider = provider or OpenAICompatibleProvider(
            OpenAICompatibleSettings(
                base_url=config.provider.base_url,
                api_key_env=config.provider.api_key_env,
                model=config.provider.model,
                temperature=config.provider.temperature,
                timeout_seconds=config.provider.timeout_seconds,
                max_output_tokens=config.provider.max_output_tokens,
                custom_parameters=config.provider.custom_parameters,
            )
        )

    def prepare_translation_runtime(self):
        """Initialize local runtime dependencies before any worker claims a file."""

        if self._token_counter is None:
            counter = build_token_counter(
                self.config.provider.tokenizer,
                self.config.cache.tokenizers,
            )
            warm_up_token_counter(counter)
            self._token_counter = counter
        return self._token_counter

    def plan(self) -> TranslationPlan:
        """构建 WebUI 预检与正式执行共同使用的只读翻译计划。"""
        validation_rule = load_validation_rule(
            self.config.rules.file,
            source_locale=self.config.languages.source,
        )
        # 即使规划本身不使用术语内容，也必须在调用 Provider 前验证维护文件。
        GlossaryRepository(self.config.glossary.file).load()
        resources = self._resources()
        if not resources:
            raise ValueError(
                f"no supported resources matched source path: {self.config.paths.source}"
            )
        with SQLiteTranslationMemory(self.config.tm.database, read_only=True) as tm:
            return TranslationPlanner(
                tm,
                validation_rule=validation_rule,
                global_exact_match=self.config.tm.global_exact_match,
                filter_rules=load_filter_rules(self.config.rules.file),
            ).build(resources, revision_materials=self._plan_revision_materials())

    def _validation_rule_for_test(self):
        """测试用：拿到 `plan()` 实际会用的那份 ValidationRule。

        接线测试要证明的是「配置里的规则真的进了判据」，直接调 loader 只能证明
        loader 会读 —— 那正是 `filter_rules=None` 那次回归躲过去的方式。
        """
        return load_validation_rule(
            self.config.rules.file, source_locale=self.config.languages.source
        )

    def rebuild_from_run(
        self,
        parent_run_id: str,
        *,
        mode: BuildMode,
        run_id: str,
        plan: Optional[TranslationPlan] = None,
    ) -> ProjectRunResult:
        """基于父运行做增量重建。

        `completed + QA failed` 不能按原 run_id resume（那条路只对执行状态
        `failed` 开放），而普通新运行**不会**复用父 checkpoint 里那些已成功但
        还没正式提交的机器译文 —— 2026-08-04 的运行提交 3 条人工修复之后，
        新计划仍有 1,427 条待翻译，只少了 4 条，等于要重复付一整轮的钱。

        这条路径：父运行不可变，创建新子运行；逐条校验源文指纹之后复用父运行的
        成功译文，叠加当前正式人工 TM，只把仍未解决的坐标送回模型，
        再跑**完整**的 QualityGate。全部失败都已人工修复时，Provider 请求数为 0。
        """
        if run_id == parent_run_id:
            raise ValueError(
                "rebuild must create a new run; parent runs are immutable"
            )
        reuse_checkpoint_run_id, parent = self._resolve_parent_checkpoint(
            parent_run_id
        )
        active_plan = plan or self.plan()
        rebuild = self._plan_rebuild(
            parent_run_id,
            reuse_checkpoint_run_id,
            parent,
            active_plan,
        )
        return self.run(
            mode=mode, run_id=run_id, plan=active_plan, rebuild=rebuild
        )

    def _resolve_parent_checkpoint(
        self, parent_run_id: str
    ) -> Tuple[str, JsonCheckpoint]:
        """沿不可变运行谱系寻找最近的可复用 checkpoint。

        零 Provider 子运行仍是本次重建的逻辑父运行；它的 task-request.json 会指向
        上一代。老版本没有为这种运行物化 checkpoint，因此需要兼容性回溯。新版本
        会为所有 rebuild 写 checkpoint，回溯主要服务于已经存在的历史运行。
        """
        runs_root = self.config.paths.workspace / "runs"
        try:
            current = validate_run_id(parent_run_id)
        except ValueError as exc:
            raise IncompatibleParentRun(str(exc)) from exc
        visited = []
        while True:
            if current in visited:
                chain = " -> ".join((*visited, current))
                raise IncompatibleParentRun(
                    f"运行谱系存在循环，无法寻找 checkpoint：{chain}"
                )
            visited.append(current)
            run_path = runs_root / current
            checkpoint_path = run_path / "checkpoint.json"
            if checkpoint_path.is_file():
                return current, JsonCheckpoint(checkpoint_path)

            request_path = run_path / "task-request.json"
            if not request_path.is_file():
                chain = " -> ".join(visited)
                raise IncompatibleParentRun(
                    f"运行 {parent_run_id} 及其祖先没有可复用的 checkpoint.json；"
                    f"已检查：{chain}"
                )
            try:
                request = json.loads(request_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise IncompatibleParentRun(
                    f"无法读取运行谱系：{request_path}: {exc}"
                ) from exc
            if not isinstance(request, Mapping):
                raise IncompatibleParentRun(
                    f"运行快照不是 JSON 对象：{request_path}"
                )
            ancestor_raw = str(request.get("parent_run_id") or "").strip()
            if not ancestor_raw:
                chain = " -> ".join(visited)
                raise IncompatibleParentRun(
                    f"运行 {parent_run_id} 没有 checkpoint，且谱系在 {current} 中断；"
                    f"已检查：{chain}"
                )
            try:
                current = validate_run_id(ancestor_raw)
            except ValueError as exc:
                raise IncompatibleParentRun(
                    f"运行谱系包含非法 parent_run_id：{ancestor_raw!r}"
                ) from exc

    def _plan_rebuild(
        self,
        parent_run_id: str,
        reuse_checkpoint_run_id: str,
        parent: JsonCheckpoint,
        active_plan: TranslationPlan,
    ) -> RebuildPlan:
        pending_ids = {unit.stable_identity: unit for unit in active_plan.pending}
        # 当前计划里已经不 pending 的，说明 TM 已经命中（含人工定稿行）——
        # 这些连复用都不需要。
        resolved_by_human = tuple(
            sorted(
                identity
                for identity, entry in parent.units.items()
                if entry.get("state") == "failed" and identity not in pending_ids
            )
        )
        fingerprints = parent.unit_fingerprints
        reused: Dict[str, str] = {}
        stale = []
        for identity, unit in pending_ids.items():
            entry = parent.units.get(identity)
            if not entry or entry.get("state") != "succeeded":
                continue
            translation = entry.get("translation")
            if not isinstance(translation, str) or not translation:
                continue
            recorded = fingerprints.get(identity)
            if recorded is None:
                # 老 checkpoint 没记指纹。**拒绝复用**而不是赌它没变 ——
                # 源文变了却复用父译文，产出的是「合法但翻的不是这句」的内容，
                # 任何 QA 规则都发现不了。
                continue
            if recorded != unit.source_fingerprint:
                stale.append(identity)
                continue
            reused[identity] = translation
        retry = tuple(
            sorted(identity for identity in pending_ids if identity not in reused)
        )
        return RebuildPlan(
            parent_run_id=parent_run_id,
            reuse_checkpoint_run_id=reuse_checkpoint_run_id,
            reused=reused,
            retry=retry,
            stale=tuple(sorted(stale)),
            resolved_by_human=resolved_by_human,
        )

    @staticmethod
    def _ordered_resource_queue(
        resource_groups: Sequence[
            Tuple[ResourceBuild, Tuple[TranslationUnit, ...]]
        ],
        *,
        checkpoint: JsonCheckpoint,
        token_counter,
    ) -> Tuple[ResourceQueueItem, ...]:
        """按 remaining source workload 对现有 resource queue 做稳定降序排列。

        这里只建立**排序视图**。已经在 checkpoint 成功的 unit 不参与评分，但
        `ResourceQueueItem.group` 仍保存原始完整 group，交给 BatchOrchestrator 后
        继续由它自己的 resume 逻辑复用成功结果。这样调度优化不会改变执行语义。
        """
        items = []
        for original_index, (_resource, group) in enumerate(resource_groups):
            remaining = tuple(
                unit
                for unit in group
                if checkpoint.succeeded(unit.stable_identity) is None
            )
            estimate_kind = "source_tokens"
            try:
                estimated_work = sum(
                    max(0, int(token_counter(unit.source_text)))
                    for unit in remaining
                )
                if remaining and estimated_work <= 0:
                    estimate_kind = "pending_units"
                    estimated_work = len(remaining)
            except Exception:
                # 排序只是性能 heuristic。计数器异常不能让一轮本可执行的翻译失败；
                # 真正的 batch planning 仍会沿既有路径使用其自己的 token 判据。
                estimate_kind = "pending_units"
                estimated_work = len(remaining)
            items.append(
                ResourceQueueItem(
                    original_index=original_index,
                    group=group,
                    relative_path=group[0].relative_path,
                    estimated_work=estimated_work,
                    remaining_units=len(remaining),
                    estimate_kind=estimate_kind,
                )
            )
        return tuple(
            sorted(
                items,
                key=lambda item: (
                    -item.estimated_work,
                    item.relative_path,
                    item.original_index,
                ),
            )
        )

    def _machine_candidate_entry(
        self, unit: TranslationUnit, translation: str, run_id: str
    ) -> TMEntry:
        """Build the single canonical SQLite shape for a machine candidate.

        Provider output and checkpoint reuse must land through exactly the same guarded
        TM write path.  Duplicating this field set would let the two release paths drift
        and recreate the split-brain state fixed by #55.
        """
        return TMEntry(
            stable_identity=unit.stable_identity,
            project_id=unit.project_id,
            adapter_id=unit.adapter_id,
            relative_path=unit.relative_path,
            logical_key=unit.logical_key,
            source_text=unit.source_text,
            source_fingerprint=unit.source_fingerprint,
            translation=translation,
            origin="machine",
            review_state="unreviewed",
            match_scope="coordinate_exact",
            run_id=run_id,
            model=self.config.provider.model,
            quality_state="passed",
            is_formal=False,
        )

    def run(
        self,
        *,
        mode: BuildMode,
        run_id: str,
        plan: Optional[TranslationPlan] = None,
        rebuild: Optional[RebuildPlan] = None,
    ) -> ProjectRunResult:
        # 规则、术语、Prompt 全部在任何 Provider 调用之前加载完。WebUI 可以把
        # 启动时刚复验过的计划传入，避免第三次扫描同一批资源。
        # R16-②：release 身份必须在**花钱之前**确认。同一份校验也在 build()
        # 里跑一次（那是唯一真正写产物的地方），这里只是把失败点提前。
        resolve_run_output(self.config.paths.output, mode=mode, run_id=run_id)
        validation_rule = load_validation_rule(
            self.config.rules.file,
            source_locale=self.config.languages.source,
        )
        glossary_terms = GlossaryRepository(self.config.glossary.file).load()
        active_plan = plan or self.plan()
        resources = active_plan.resources
        all_units = tuple(unit for resource in resources for unit in resource.units)
        translations = dict(active_plan.translations)
        # 译文来源贯穿到 QA：QualityGate 靠它区分「本次新增的缺陷」与「搬运过来的存量债」。
        provenance = dict(active_plan.provenance)
        # checkpoint_candidates 是本轮规划时仍需机器结果的完整集合。rebuild 复用
        # 会从真正的 Provider pending 中移除一部分，但这些结果仍必须物化进子
        # checkpoint，否则下一代无法以这个最新子运行为父运行。
        checkpoint_candidates = tuple(active_plan.pending)
        checkpoint_candidate_by_identity = {
            unit.stable_identity: unit for unit in checkpoint_candidates
        }
        pending = list(checkpoint_candidates)
        if rebuild is not None and rebuild.reused:
            # 复用的译文是父运行**这一轮**产出的机器译文，provenance 仍是
            # machine —— 它对 QualityGate 是零容忍的增量内容，不能因为
            # 换了个 run_id 就降格成存量债。
            translations.update(rebuild.reused)
            for identity in rebuild.reused:
                provenance[identity] = PROVENANCE_THIS_RUN
            pending = [
                unit
                for unit in pending
                if unit.stable_identity not in rebuild.reused
            ]
        provider_scope_units = len(pending)
        provider_scope_files = tuple(sorted({unit.relative_path for unit in pending}))
        tm_hits = active_plan.tm_hits
        token_counter = self.prepare_translation_runtime() if pending else None

        checkpoint: Optional[JsonCheckpoint] = None
        if pending or rebuild is not None:
            checkpoint = JsonCheckpoint(
                self.config.paths.workspace / "runs" / run_id / "checkpoint.json"
            )
            if rebuild is not None:
                self._record_lineage(run_id, rebuild)
            checkpoint_ids = {
                unit.stable_identity for unit in checkpoint_candidates
            }
            checkpoint_resource_groups = [
                (
                    resource,
                    tuple(
                        unit
                        for unit in resource.units
                        if unit.stable_identity in checkpoint_ids
                    ),
                )
                for resource in resources
            ]
            checkpoint_resource_groups = [
                item for item in checkpoint_resource_groups if item[1]
            ]
            checkpoint.configure_run(
                translation_units_total=len(checkpoint_candidates),
                translation_files_total=len(checkpoint_resource_groups),
                resource_units={
                    group[0].relative_path: [
                        unit.stable_identity for unit in group
                    ]
                    for _resource, group in checkpoint_resource_groups
                },
                unit_fingerprints={
                    unit.stable_identity: unit.source_fingerprint
                    for unit in checkpoint_candidates
                },
            )
            if rebuild is not None and rebuild.reused:
                for identity, translation in rebuild.reused.items():
                    checkpoint.record_result(
                        UnitResult(identity, translation, "succeeded")
                    )
                retry_ids = {unit.stable_identity for unit in pending}
                for resource, group in checkpoint_resource_groups:
                    if not any(
                        unit.stable_identity in retry_ids for unit in group
                    ):
                        checkpoint.complete_resource(
                            "rebuild-reuse", group[0].relative_path
                        )
                checkpoint.flush_now()

        with SQLiteTranslationMemory(self.config.tm.database) as tm:
            # `machine_success_ids` remains an execution metric: it counts only Provider
            # successes generated in this run.  Promotion is a different concern, because a
            # release rebuild may ship zero-new-work translations safely reused from a parent.
            machine_success_ids = []
            promotable_machine_ids = []
            staged = []
            failed_ids = []
            root_causes: Dict[str, Dict[str, object]] = {}

            # #55: a release artifact is authoritative only if the machine translations it
            # actually ships can become the next formal TM baseline.  Reused preview
            # candidates therefore get re-attributed to this *release* run through the same
            # guarded upsert path as fresh Provider output.  Preview rebuilds deliberately do
            # not do this; they remain non-authoritative candidates.
            if mode is BuildMode.RELEASE and rebuild is not None and rebuild.reused:
                for identity, translation in rebuild.reused.items():
                    unit = checkpoint_candidate_by_identity.get(identity)
                    if unit is None:
                        raise IncompatibleParentRun(
                            "rebuild reuse contains an identity outside the active pending plan: "
                            f"{identity}"
                        )
                    staged.append(
                        self._machine_candidate_entry(unit, translation, run_id)
                    )
                    promotable_machine_ids.append(identity)

            if pending:
                prompt = self.config.prompt.template.read_text(encoding="utf-8")
                background = (
                    self.config.prompt.background.read_text(encoding="utf-8")
                    if self.config.prompt.background
                    else ""
                )
                glossary = "\n".join(
                    f"{term.source} => {term.target}" for term in glossary_terms
                )
                assert checkpoint is not None
                pending_ids = {unit.stable_identity for unit in pending}
                resource_groups = [
                    (
                        resource,
                        tuple(
                            unit
                            for unit in resource.units
                            if unit.stable_identity in pending_ids
                        ),
                    )
                    for resource in resources
                ]
                resource_groups = [item for item in resource_groups if item[1]]
                by_identity = {unit.stable_identity: unit for unit in pending}
                worker_count = min(
                    self.config.provider.concurrency, len(resource_groups)
                )
                ordered_queue = self._ordered_resource_queue(
                    resource_groups,
                    checkpoint=checkpoint,
                    token_counter=token_counter,
                )
                work_queue: Queue[Tuple[int, Tuple]] = Queue()
                for item in ordered_queue:
                    work_queue.put((item.original_index, item.group))
                composer = PromptComposer(prompt, background, glossary)

                def translate_worker(worker_index: int):
                    orchestrator = BatchOrchestrator(
                        self.provider,
                        composer,
                        checkpoint,
                        validation_rule=validation_rule,
                        context_window=self.config.provider.context_window,
                        max_output_tokens=self.config.provider.max_output_tokens,
                        token_counter=token_counter,
                        max_requests=max(
                            16, (len(pending) * 8 + worker_count - 1) // worker_count
                        ),
                    )
                    completed = []
                    while True:
                        try:
                            resource_index, group = work_queue.get_nowait()
                        except Empty:
                            break
                        try:
                            completed.append(
                                (
                                    resource_index,
                                    orchestrator.run(
                                        group,
                                        resource_path=group[0].relative_path,
                                        worker_id=f"translation-{worker_index}",
                                    ),
                                )
                            )
                        finally:
                            work_queue.task_done()
                    return completed

                completed_results = {}
                # 逐条结果的落盘按时间合并过，收尾必须强制写出最后一个窗口，
                # 否则断点恢复时会白白重译窗口内那几条。**必须在 finally 里** ——
                # 任何一个 worker 抛出都会经 future.result() 重抛，走直线代码的
                # finalize() 就永远到不了，窗口里那批已经付过钱的译文全部丢失。
                try:
                    with ThreadPoolExecutor(
                        max_workers=worker_count,
                        thread_name_prefix="localizer-translation",
                    ) as executor:
                        futures = [
                            executor.submit(translate_worker, worker_index)
                            for worker_index in range(worker_count)
                        ]
                        for future in as_completed(futures):
                            for resource_index, batch_result in future.result():
                                completed_results[resource_index] = batch_result
                finally:
                    checkpoint.finalize()

                for resource_index in range(len(resource_groups)):
                    batch_result = completed_results[resource_index]
                    for result in batch_result.results:
                        unit = by_identity[result.stable_identity]
                        if (
                            result.state == "succeeded"
                            and result.translation is not None
                        ):
                            translations[result.stable_identity] = result.translation
                            # 本次运行自己产出的译文：QualityGate 对它零容忍。
                            provenance[result.stable_identity] = PROVENANCE_THIS_RUN
                            machine_success_ids.append(result.stable_identity)
                            promotable_machine_ids.append(result.stable_identity)
                            staged.append(
                                self._machine_candidate_entry(
                                    unit, result.translation, run_id
                                )
                            )
                        else:
                            failed_ids.append(result.stable_identity)
                            # R03：把「为什么没译出来」留下来。编排层的
                            # QAIssue（Provider 报错、内容 QA 判据）此前在
                            # 这里被整个丢掉，报告只剩一条 `empty_translation`，
                            # 运维无从判断该重试还是该改 prompt。
                            root_causes[result.stable_identity] = {
                                "state": result.state,
                                "issues": [
                                    {
                                        "code": issue.code,
                                        "severity": issue.severity,
                                        "message": issue.message,
                                    }
                                    for issue in result.issues
                                ],
                            }

            # Run all machine candidates through the existing physical TM guards in one
            # transaction.  This is intentionally outside `if pending`: a zero-Provider
            # release rebuild still has reused candidates that must be attributable to the
            # current release before QualityGate is allowed to promote them.
            rejected = tm.upsert_many(staged)
            if rejected:
                # 这些坐标在 TM 里已是 formal，但源文指纹与本次不符，
                # 于是 upsert 的 WHERE 把写入**静默挡下**（零异常、零 rowcount
                # 信号）。原来这件事要一直拖到 render 之后的
                # `validate_promotable_run` 才炸「cannot promote entries absent
                # from the requested run」—— 那时制品已经渲染完，而同 run_id
                # 又不许重试，整轮白跑。把失败点提到 render 之前。
                preview = ", ".join(sorted(rejected)[:10])
                more = f"（另有 {len(rejected) - 10} 条）" if len(rejected) > 10 else ""
                variant_hint = (
                    f" --variant {self.config.active_variant}"
                    if self.config.active_variant
                    else ""
                )
                raise StaleFormalEntryError(
                    f"{len(rejected)} 个坐标在 TM 里已是 formal，但源文已经变了，"
                    f"本次翻译结果无法写入：{preview}{more}\n"
                    f"这些是上一轮 release 晋升过、之后源文又被改动的条目。"
                    f"先用 `localizer review-retire --stale <config>"
                    f"{variant_hint}` 预览，再加 `--apply` 清理后恢复同一 run_id。",
                    rejected,
                )

            lineage = self._run_lineage(rebuild.parent_run_id) if rebuild else ()
            current_metrics = (
                dict(checkpoint.metrics)
                if checkpoint
                else {
                    "requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "translation_units_total": 0,
                    "translation_files_total": 0,
                }
            )
            has_current_provider_work = int(current_metrics.get("requests", 0) or 0) > 0
            current_run_metrics = dict(current_metrics)
            current_run_metrics["translation_units_total"] = (
                provider_scope_units if has_current_provider_work else 0
            )
            current_run_metrics["translation_files_total"] = (
                len(provider_scope_files) if has_current_provider_work else 0
            )
            if has_current_provider_work and provider_scope_files:
                current_run_metrics["translation_files"] = list(provider_scope_files)
            current_record = normalize_execution_record(
                run_id,
                current_run_metrics,
                translation_files=(
                    provider_scope_files if has_current_provider_work else ()
                ),
            )
            evidence_store = TranslationEvidenceStore(
                self.config.paths.workspace / "runs"
            )
            inherited_evidence = (
                evidence_store.inherited_for_rebuild(
                    parent_run_id=rebuild.parent_run_id,
                    reuse_checkpoint_run_id=rebuild.reuse_checkpoint_run_id,
                    lineage=lineage,
                    reused_count=len(rebuild.reused),
                )
                if rebuild
                else ()
            )
            evidence_records = list(inherited_evidence)
            if current_record["requests"] > 0:
                evidence_records.append(current_record)
            if checkpoint is not None or evidence_records:
                evidence_records = list(evidence_store.save(run_id, evidence_records))
            aggregate_metrics = aggregate_execution_metrics(evidence_records)
            # Preserve checkpoint-only operational fields (`completed_files`, degraded-write
            # diagnostics, etc.) for existing consumers while replacing the public execution
            # counters with the deduplicated contributing-run values.
            public_metrics = {**current_metrics, **aggregate_metrics}

            build = LocalBuildPipeline(
                validation_rule=validation_rule,
                glossary_terms=glossary_terms,
                quality_gate=QualityGate(
                    LegacyDebtBaseline.load(
                        self.config.quality_gate.legacy_debt_baseline
                    )
                ),
            ).build(
                resources,
                translations,
                mode=mode,
                project_id=self.config.project.id,
                run_id=run_id,
                output_root=self.config.paths.output,
                failed_unit_identities=failed_ids,
                unit_provenance=provenance,
                unit_root_causes=root_causes,
                filtered_identities=tuple(active_plan.filtered),
                tm=tm,
                formal_tm_identities=promotable_machine_ids,
                manifest_metadata={
                    **self._manifest_metadata(resources, tm),
                    # Backward-compatible public summary: requests/tokens now describe the
                    # deduplicated Provider executions that actually contribute translations
                    # to this release lineage, not merely the zero-work child run.
                    "translation_metrics": public_metrics,
                    "translation_metrics_current_run": current_run_metrics,
                    "translation_evidence_runs": evidence_records,
                    "translation_metrics_scope": "contributing_run_execution",
                    # 这一轮批次是怎么跑的。metrics 只有总量，回答不了
                    # 「为什么贵」和「有没有缩过批」。
                    "batch_summary": (
                        checkpoint.batch_summary() if checkpoint else {}
                    ),
                    # 子运行必须能追到父运行，以及复用/重试各多少。
                    # lineage 是**整条**祖先链，不只是直接父运行 —— 连续重建
                    # 之后「这个包到底基于哪一轮的钱」只有整条链能回答。
                    **(
                        {
                            "rebuild": {
                                **rebuild.as_dict(),
                                "lineage": list(lineage),
                            }
                        }
                        if rebuild
                        else {}
                    ),
                },
                artifact_options={
                    "version": self.config.project.game_version,
                    "variant": self.config.build.variant,
                    "artifact_prefix": self.config.build.artifact_prefix,
                    "compression": self.config.build.compression,
                    "encryption": self.config.build.encryption,
                    "password_env": self.config.build.password_env,
                    "archive_root": self.config.build.archive_root,
                    "compatibility_metadata": (
                        self.config.build.compatibility_metadata.model_dump()
                        if hasattr(self.config.build.compatibility_metadata, "model_dump")
                        else self.config.build.compatibility_metadata.dict()
                    ),
                },
            )
        return ProjectRunResult(
            build,
            len(all_units),
            tm_hits,
            len(machine_success_ids),
            len(failed_ids),
            rebuild,
        )

    LINEAGE_FILENAME = "run-lineage.json"

    def _record_lineage(self, run_id: str, rebuild: "RebuildPlan") -> None:
        """把父运行链接写进 runner **自己的**产物里。

        谱系原本只能从 `task-request.json` 读，而那是 **WebUI** 写的文件：
        CLI 的 `rebuild-from-run` 根本不写它，于是「整条祖先链」这个说法只对
        面板创建的运行成立，命令行连续两代重建的链条在第一跳就断了 ——
        而发布说明照样只印一个 id，看起来像是"就只有一代"。
        """
        path = self.config.paths.workspace / "runs" / run_id / self.LINEAGE_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        AtomicIO.write_json(
            path,
            {
                "schema_version": 1,
                "run_id": run_id,
                "parent_run_id": rebuild.parent_run_id,
                "reuse_checkpoint_run_id": rebuild.reuse_checkpoint_run_id,
            },
        )

    def _run_lineage(self, run_id: str, *, limit: int = 32) -> Tuple[str, ...]:
        """沿父运行链往上走，返回祖先链（近到远）。

        两个来源：runner 自己写的 `run-lineage.json`（CLI 与 WebUI 都有），
        以及 WebUI 的 `task-request.json`（老运行只有这个）。先读前者。

        只读、容错：谱系断了就到此为止，不抛异常。它是发布说明里的说明性信息，
        不是闸门 —— 拿不到完整链条不该让一次合法发布失败。`limit` 只为防挂住，
        真正的环检测在 `_resolve_parent_checkpoint`。
        """
        runs_root = self.config.paths.workspace / "runs"
        chain = []
        current = run_id
        while current and current not in chain and len(chain) < limit:
            chain.append(current)
            current = self._parent_of(runs_root / current)
        if len(chain) >= limit:
            # 静默截断会让读的人以为那就是全部。
            chain.append("…(谱系被截断)")
        return tuple(chain)

    @staticmethod
    def _parent_of(run_path: Path) -> str:
        for name in (ProjectRunner.LINEAGE_FILENAME, "task-request.json"):
            candidate = run_path / name
            if not candidate.is_file():
                continue
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, Mapping):
                continue
            parent = str(payload.get("parent_run_id") or "").strip()
            if parent:
                return parent
        return ""

    def _plan_revision_materials(self) -> Tuple[bytes, ...]:
        config_data = (
            self.config.model_dump()
            if hasattr(self.config, "model_dump")
            else self.config.dict()
        )
        materials = [
            json.dumps(config_data, sort_keys=True, default=str).encode("utf-8"),
            self.config.prompt.template.read_bytes(),
            self.config.glossary.file.read_bytes(),
            self.config.rules.file.read_bytes(),
        ]
        if self.config.prompt.background is not None:
            materials.append(self.config.prompt.background.read_bytes())
        return tuple(materials)

    def _resources(self) -> Sequence[ResourceBuild]:
        """按配置构造 Adapter 并规划要处理的资源。

        两趟：先让所有 Adapter 对所有命中文件表态，按 (probe 置信度降序,
        配置顺序升序) 择优；再对每个文件用胜出的 Adapter 做 extract。
        原实现是「绝对路径 + 配置书写顺序先者赢」的隐式优先级，
        ResourceDescriptor.confidence 完全没被用来仲裁，而 §7.2 明确要求
        「支持 include、exclude 和 Adapter 优先级」。
        """
        scanner = ResourceScanner()
        # path -> (confidence, -配置顺序, adapter)
        best: Dict[Path, Tuple[float, int, object]] = {}
        for order, configured in enumerate(self.config.resources.adapters):
            adapter = build_adapter(
                configured.type,
                project_id=self.config.project.id,
                source_root=self.config.paths.source,
                source_locale=self.config.languages.source,
                target_locale=self.config.languages.target,
                options=configured.options,
            )
            scan = scanner.scan(
                self.config.paths.source,
                includes=configured.include,
                excludes=configured.exclude,
            )
            for resource in scan.resources:
                confidence = adapter.probe(resource.absolute_path)
                if confidence <= 0:
                    continue
                candidate = (confidence, -order, adapter)
                current = best.get(resource.absolute_path)
                if current is None or candidate[:2] > current[:2]:
                    best[resource.absolute_path] = candidate

        planned = [
            ResourceBuild(adapter, path, tuple(adapter.extract(path)))
            for path, (_confidence, _order, adapter) in sorted(
                best.items(), key=lambda item: str(item[0])
            )
        ]
        return tuple(planned)

    def _manifest_metadata(
        self,
        resources: Sequence[ResourceBuild],
        tm: SQLiteTranslationMemory,
    ) -> dict:
        source_hasher = sha256()
        for resource in sorted(
            resources, key=lambda item: item.adapter.scan(item.source).relative_path
        ):
            relative = resource.adapter.scan(resource.source).relative_path
            source_hasher.update(relative.encode("utf-8"))
            source_hasher.update(resource.source.read_bytes())

        def digest(path: Path) -> str:
            return sha256(Path(path).read_bytes()).hexdigest()

        metadata = {
            "game_version": self.config.project.game_version,
            "release_channel": self.config.build.release_channel,
            "workflow_mode": self.config.workflow.mode,
            "quality_level": "quality_gate_passed",
            "source_fingerprint": source_hasher.hexdigest(),
            "tm_revision": f"sqlite-schema-1-authoritative-{str(tm.is_authoritative()).lower()}",
            "glossary_revision": digest(self.config.glossary.file),
            "rules_revision": digest(self.config.rules.file),
            "prompt_revision": digest(self.config.prompt.template),
            "provider": {
                "type": self.config.provider.type,
                "model": self.config.provider.model,
                "context_window": self.config.provider.context_window,
                "max_output_tokens": self.config.provider.max_output_tokens,
                "custom_parameters": dict(self.config.provider.custom_parameters),
            },
        }
        if self.config.provider.tokenizer is not None:
            metadata["provider"]["tokenizer"] = {
                "type": self.config.provider.tokenizer.type,
                "model": self.config.provider.tokenizer.model,
                "revision": self.config.provider.tokenizer.revision,
            }
        if self.config.workflow.mode == "paratranz":
            metadata["paratranz_project_id"] = self.config.workflow.project_id
        return metadata