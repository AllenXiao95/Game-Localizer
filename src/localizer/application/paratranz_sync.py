from __future__ import annotations

import csv
import io
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple

import yaml

from localizer.infrastructure.atomic_io import AtomicIO
from localizer.adapters.storage.sqlite_tm import TMEntry
from localizer.domain.translation_unit import TranslationUnit


VALID_STAGES = {-1, 0, 1, 2, 3, 5, 9}
AUTOMATIC_TM_STAGES = {3, 5, 9}
MACHINE_PROTECTED_STAGES = {-1, 2, 3, 5, 9}


@dataclass(frozen=True)
class ParaTranzItem:
    key: str
    original: str
    translation: str
    stage: int
    origin: str = "remote"

    def __post_init__(self) -> None:
        if self.stage not in VALID_STAGES:
            raise ValueError(f"unsupported ParaTranz stage: {self.stage}")


@dataclass(frozen=True)
class StageMeaning:
    label: str
    automatic_tm: bool
    candidate_only: bool = False
    hidden: bool = False


class ParaTranzStagePolicy:
    MEANINGS = {
        0: StageMeaning("untranslated", False),
        1: StageMeaning("translated_unreviewed", False, candidate_only=True),
        2: StageMeaning("questionable", False),
        3: StageMeaning("checked", True),
        5: StageMeaning("reviewed", True),
        9: StageMeaning("locked", True),
        -1: StageMeaning("hidden", False, hidden=True),
    }

    def meaning(self, stage: int) -> StageMeaning:
        try:
            return self.MEANINGS[stage]
        except KeyError as exc:
            raise ValueError(f"unsupported ParaTranz stage: {stage}") from exc

    def can_use_as_automatic_tm(self, stage: int) -> bool:
        return self.meaning(stage).automatic_tm

    def is_machine_protected(self, stage: int) -> bool:
        return stage in MACHINE_PROTECTED_STAGES

    def to_tm_entry(self, unit: TranslationUnit) -> TMEntry:
        stage = unit.metadata.get("stage", 0)
        if stage not in VALID_STAGES:
            raise ValueError(f"unsupported ParaTranz stage: {stage}")
        review_state = {
            0: "untranslated",
            1: "unreviewed",
            2: "suspect",
            3: "checked",
            5: "reviewed",
            9: "locked",
            -1: "hidden",
        }[stage]
        formal = self.can_use_as_automatic_tm(stage) and bool(
            (unit.translation or "").strip()
        )
        return TMEntry(
            stable_identity=unit.stable_identity,
            project_id=unit.project_id,
            adapter_id=unit.adapter_id,
            relative_path=unit.relative_path,
            logical_key=unit.logical_key,
            source_text=unit.source_text,
            source_fingerprint=unit.source_fingerprint,
            translation=unit.translation or "",
            origin="paratranz",
            review_state=review_state,
            match_scope="coordinate_exact",
            classification="paratranz",
            stage=stage,
            quality_state="passed" if formal else "candidate",
            is_formal=formal,
            # stage 非 0 即代表有人在 ParaTranz 上动过：1 已译未审、2 存疑、
            # 3/5/9 已检查/已审核/已锁定、-1 人工隐藏。这些都不得被机器译文覆盖，
            # 而其中 1/2/-1 的 is_formal 是 0，仅靠 is_formal 挡不住（§16）。
            human_authored=stage != 0,
        )


@dataclass(frozen=True)
class SyncConflict:
    key: str
    reason: str
    baseline: Optional[ParaTranzItem]
    local: Optional[ParaTranzItem]
    remote: Optional[ParaTranzItem]

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "reason": self.reason,
            "baseline": asdict(self.baseline) if self.baseline else None,
            "local": asdict(self.local) if self.local else None,
            "remote": asdict(self.remote) if self.remote else None,
        }


@dataclass(frozen=True)
class MergeResult:
    merged: Tuple[ParaTranzItem, ...]
    uploads: Tuple[ParaTranzItem, ...]
    conflicts: Tuple[SyncConflict, ...]


class ThreeWayParaTranzMerger:
    """Offline P07/P08 policy. It never calls the ParaTranz API."""

    def __init__(self, policy: Optional[ParaTranzStagePolicy] = None) -> None:
        self.policy = policy or ParaTranzStagePolicy()

    def merge(
        self,
        baseline: Sequence[ParaTranzItem],
        local: Sequence[ParaTranzItem],
        remote: Sequence[ParaTranzItem],
        *,
        resolutions: Optional[Mapping[str, str]] = None,
    ) -> MergeResult:
        base = self._index(baseline)
        local_by_key = self._index(local)
        remote_by_key = self._index(remote)
        decisions: Dict[str, ParaTranzItem] = {}
        uploads = []
        conflicts = []
        resolution_map = dict(resolutions or {})
        for key in sorted(set(base) | set(local_by_key) | set(remote_by_key)):
            before = base.get(key)
            local_item = local_by_key.get(key)
            remote_item = remote_by_key.get(key)
            resolved = resolution_map.get(key)
            if resolved:
                if resolved not in {"local", "remote"}:
                    raise ValueError(f"resolution for {key!r} must be local or remote")
                selected = local_item if resolved == "local" else remote_item
                if selected is None:
                    raise ValueError(f"resolution for {key!r} selected a missing side")
                decisions[key] = selected
                if resolved == "local":
                    upload = self._as_machine_candidate(selected)
                    uploads.append(upload)
                    decisions[key] = upload
                continue
            if remote_item is None:
                if local_item is not None:
                    upload = self._as_machine_candidate(local_item)
                    uploads.append(upload)
                    decisions[key] = upload
                continue

            # 所有「采纳远端」的分支都必须走这里。
            #
            # ParaTranz 会把未翻译词条一并导出，translation 为空串；分页缺字段、
            # 审校误清空也会产生空串。若照单全收，一条已定稿的人工译文会被无声删除，
            # 下游 to_tm_entry 还会把空串写回 TM —— 这正是 tools/paratranz.py 修过的
            # 那次事故（audit/paratranz_regression_test.py 12 项守着它）。§16 要求本地
            # 与 ParaTranz 共用同一核心，新内核必须承接这个修复。
            #
            # 闸门只拦「静默清空」：显式 resolutions 在上面已经 continue，人工裁决
            # 「用远端」仍然生效；本地机器候选该上传的路径也不受影响（见下方 upload 分支）。
            def adopt_remote() -> ParaTranzItem:
                if not remote_item.translation.strip() and (
                    local_item is not None and local_item.translation.strip()
                ):
                    conflicts.append(
                        SyncConflict(
                            key,
                            "remote_empty_would_clear_local",
                            before,
                            local_item,
                            remote_item,
                        )
                    )
                    return local_item
                return remote_item

            if remote_item.stage == -1:
                decisions[key] = adopt_remote()
                continue
            if local_item is None:
                decisions[key] = remote_item
                continue
            if remote_item.original != local_item.original:
                conflicts.append(
                    SyncConflict(key, "source_changed", before, local_item, remote_item)
                )
                decisions[key] = adopt_remote()
                continue

            local_changed = self._translation_changed(before, local_item)
            remote_changed = self._translation_changed(before, remote_item)
            if self.policy.is_machine_protected(remote_item.stage):
                decisions[key] = adopt_remote()
                continue
            if remote_item.stage == 1 and remote_item.translation.strip():
                decisions[key] = remote_item
                continue
            if local_changed and remote_changed and local_item.translation != remote_item.translation:
                # Machine candidates never defeat a non-empty remote result. Other simultaneous
                # changes are surfaced for ParaTranz-side human resolution.
                if local_item.origin == "machine" and remote_item.translation.strip():
                    decisions[key] = remote_item
                else:
                    conflicts.append(
                        SyncConflict(
                            key, "both_sides_changed", before, local_item, remote_item
                        )
                    )
                    decisions[key] = adopt_remote()
                continue
            if local_changed and local_item.translation.strip():
                upload = self._as_machine_candidate(local_item)
                uploads.append(upload)
                decisions[key] = upload
            else:
                decisions[key] = adopt_remote()
        return MergeResult(
            tuple(decisions[key] for key in sorted(decisions)),
            tuple(uploads),
            tuple(conflicts),
        )

    @staticmethod
    def _translation_changed(
        baseline: Optional[ParaTranzItem], current: ParaTranzItem
    ) -> bool:
        if baseline is None:
            return bool(current.translation.strip())
        return current.translation != baseline.translation

    @staticmethod
    def _as_machine_candidate(item: ParaTranzItem) -> ParaTranzItem:
        return ParaTranzItem(
            item.key,
            item.original,
            item.translation,
            1,
            origin="machine",
        )

    @staticmethod
    def _index(items: Iterable[ParaTranzItem]) -> Dict[str, ParaTranzItem]:
        result = {}
        for item in items:
            if item.key in result:
                raise ValueError(f"duplicate ParaTranz key: {item.key}")
            result[item.key] = item
        return result


class ConflictReportWriter:
    @staticmethod
    def write(directory: Path, conflicts: Sequence[SyncConflict]) -> Tuple[Path, Path]:
        destination = Path(directory).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        json_path = destination / "conflicts.json"
        csv_path = destination / "conflicts.csv"
        AtomicIO.write_json(json_path, [item.to_dict() for item in conflicts])
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "key",
                "reason",
                "baseline_translation",
                "local_translation",
                "remote_translation",
                "remote_stage",
            ]
        )
        for item in conflicts:
            writer.writerow(
                [
                    item.key,
                    item.reason,
                    item.baseline.translation if item.baseline else "",
                    item.local.translation if item.local else "",
                    item.remote.translation if item.remote else "",
                    item.remote.stage if item.remote else "",
                ]
            )
        AtomicIO.write_text(csv_path, buffer.getvalue())
        return json_path, csv_path


# 冲突裁决文件当前只有一个版本。新增版本时在这里登记，并保留旧版本的读取分支。
SUPPORTED_RESOLUTION_SCHEMA_VERSIONS = (1,)


def load_resolutions(path: Path) -> Mapping[str, str]:
    """读取版本化的冲突裁决文件。

    版本协商是硬要求，不是装饰：这个文件决定「本地译文还是远端译文进制品」，
    而它是人手工编辑的。格式一旦演进（例如加入「按 stage 分档」或「三方标注」），
    旧读法会把新语义**静默读错**，产出的却是看起来正常的制品。

    原来这里既不读也不校验 `schema_version`，四种输入实测全部被接受：
    无版本字段、`schema_version: 99`、`schema_version: "not-a-number"`，以及
    `schema_version: 1` 配扁平键 —— 最后一种会把版本号本身当成一条裁决，
    抛出「resolution values must be local or remote」这种完全误导的错误。
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("resolution file root must be a mapping")
    version = raw.get("schema_version")
    if version not in SUPPORTED_RESOLUTION_SCHEMA_VERSIONS:
        supported = "/".join(str(v) for v in SUPPORTED_RESOLUTION_SCHEMA_VERSIONS)
        raise ValueError(
            f"resolution file {path}: unsupported schema_version {version!r}; "
            f"supported: {supported}"
        )
    if "resolutions" not in raw:
        # 扁平映射不再兜底：它与「忘了写 resolutions 这层」无法区分，
        # 而两者的正确处理方式相反（一个该读，一个该报错）。
        raise ValueError(f"resolution file {path}: missing required key 'resolutions'")
    resolutions = raw["resolutions"]
    if not isinstance(resolutions, Mapping):
        raise ValueError("resolution entries must be a mapping")
    result = {str(key): str(value) for key, value in resolutions.items()}
    invalid = sorted({value for value in result.values() if value not in {"local", "remote"}})
    if invalid:
        raise ValueError(
            "resolution values must be local or remote; got: " + ", ".join(invalid)
        )
    return result
