from __future__ import annotations

import csv
import io
import json
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Sequence, Set, Tuple

from localizer.infrastructure.atomic_io import AtomicIO

# 本次运行自己产出的译文。这类问题零容忍 —— 是我们刚生成的，没有任何理由放行。
PROVENANCE_THIS_RUN = "machine"

# 本次运行没有产出、只是照单搬运的译文：TM 命中、ParaTranz 回流、资源里自带的
# 已有译文。它们的缺陷属于**存量债**，与本次增量无关。
CARRIED_PROVENANCES = (
    "coordinate_exact",
    "legacy_coordinate_exact",
    "legacy_source_converged",
    "paratranz_coordinate_exact",
    "reviewed_source_exact",
    "embedded",
    "unknown",
)


def is_this_run(provenance: Optional[str]) -> bool:
    return (provenance or "unknown") == PROVENANCE_THIS_RUN


@dataclass(frozen=True)
class QARecord:
    code: str
    severity: str
    message: str
    stable_identity: Optional[str] = None
    relative_path: Optional[str] = None
    details: Optional[Mapping[str, object]] = None
    # 这条问题出在谁产出的译文上。决定它算增量缺陷还是存量债。
    provenance: str = "unknown"

    @property
    def debt_key(self) -> str:
        """存量债基线里的稳定键。

        必须带上 details 维度。只用 (词条, code) 会坍缩：真机 851 条
        `glossary_violation` 只压成 790 个键，53 个词条同时承载 2 条**不同术语**的
        违规（例如 `dogtags.mo/.../31004/description` 同时违反 Огненный волк 与
        Серебро），共 114 条。后果是「同一词条同一 code 上出现一条全新坏账」
        免检放行 —— 登记 849 条债之后新增一条 reviewed 术语，53 个已登记词条各自
        新增一条针对新术语的违规，全部静默通过，**棘轮语义「债只能减不能增」
        被击穿**。details 用规范化 JSON 摘要，键长可控且与字段顺序无关。
        """
        digest = sha256(
            json.dumps(
                self.details or {}, ensure_ascii=False, sort_keys=True, default=str
            ).encode("utf-8")
        ).hexdigest()[:12]
        return f"{self.stable_identity or ''}::{self.code}::{digest}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["details"] = dict(self.details or {})
        return data


@dataclass(frozen=True)
class QualityGateResult:
    passed: bool
    error_count: int
    failed_unit_count: int
    # 本次运行自己产出的译文上的 error。零容忍。
    new_error_count: int = 0
    # 存量债：搬运过来的译文上的 error。
    carried_error_count: int = 0
    # 存量债里**不在已接受基线内**的部分。这才是阻断依据。
    unaccepted_carried_count: int = 0
    accepted_debt_count: int = 0
    baseline_path: Optional[str] = None

    def as_dict(self) -> dict:
        return asdict(self)


class QualityGateError(RuntimeError):
    """A normal release rejection with a machine-readable gate result."""

    def __init__(self, message: str, result: QualityGateResult) -> None:
        super().__init__(message)
        self.result = result


class LegacyDebtBaseline:
    """已接受的存量债基线 —— 一把棘轮。

    问题背景：QualityGate 原本对全量 error 零容忍。真机 preview 实测 853 个 error
    里 **849 个来自历史 TM 命中，只有 2 个来自本次机器新译**（见
    docs/preview-validation-20260802.md §6 P1-1）。这意味着只要历史 TM 里还有一条
    坏账，任何 release 都发不出去 —— 增量打包被存量债永久阻塞。

    但不能靠降低严重度放行：那正是这个项目反复出现的「失败伪装成成功」。
    正确解法是让存量债**显式记账**：

    - 本次运行自己产出的译文（machine）上的 error：**永远零容忍**；
    - 搬运过来的译文上的 error：只有已登记在基线文件里的才放行，
      **新增的一律阻断** —— 债只能减不能增；
    - 基线文件进版本库，谁接受了哪些债、什么时候接受的，都可 review、可回溯。
    """

    # v2：debt_key 加入 details 摘要。**不兼容 v1** —— v1 的键在新算法下必然全部
    # 失配，静默兼容等于「基线里一条都对不上」，那会让所有存量债重新变成未登记
    # 而阻断 release（fail-closed，不危险但会让人以为棘轮坏了）。明确拒绝并提示
    # 重新生成，比任何一种默默处理都清楚。
    SCHEMA_VERSION = 2

    def __init__(self, accepted: Optional[Iterable[str]] = None,
                 *, path: Optional[Path] = None) -> None:
        self.accepted: Set[str] = set(accepted or ())
        self.path = Path(path) if path else None

    @classmethod
    def load(cls, path: Optional[Path]) -> "LegacyDebtBaseline":
        if path is None:
            return cls()
        target = Path(path)
        if not target.is_file():
            return cls(path=target)
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"cannot read legacy debt baseline {target}: {exc}") from exc
        if not isinstance(raw, Mapping) or raw.get("schema_version") != cls.SCHEMA_VERSION:
            found = raw.get("schema_version") if isinstance(raw, Mapping) else None
            hint = ""
            if found == 1:
                hint = (
                    "；v1 的键不含 details 摘要，在 v2 下必然全部失配，"
                    "请用 `localizer qa-accept-debt` 基于最新 QA 报告重新生成"
                )
            raise ValueError(
                f"legacy debt baseline {target} must use schema_version: "
                f"{cls.SCHEMA_VERSION} (found: {found!r}){hint}"
            )
        entries = raw.get("accepted", [])
        if not isinstance(entries, list) or not all(isinstance(x, str) for x in entries):
            raise ValueError(f"legacy debt baseline {target}: accepted must be a list of strings")
        return cls(entries, path=target)

    def accepts(self, record: QARecord) -> bool:
        return record.debt_key in self.accepted

    def write(self, path: Path, records: Sequence[QARecord], *, note: str = "") -> Path:
        """把当前的存量债快照成基线。只收 carried 且 severity=error 的记录。"""
        carried = sorted(
            {r.debt_key for r in records if r.severity == "error" and not is_this_run(r.provenance)}
        )
        by_code: Dict[str, int] = {}
        for record in records:
            if record.severity == "error" and not is_this_run(record.provenance):
                by_code[record.code] = by_code.get(record.code, 0) + 1
        return AtomicIO.write_json(
            Path(path),
            {
                "schema_version": self.SCHEMA_VERSION,
                "note": note or (
                    "已接受的存量债。只减不增：新出现的 carried error 会阻断 release。"
                    "清理一条就从这里删一条。"
                ),
                "accepted_total": len(carried),
                "by_code": dict(sorted(by_code.items())),
                "accepted": carried,
            },
        )


class QualityGate:
    """release 对**本次新增**零容忍；存量债按基线棘轮放行。"""

    def __init__(self, baseline: Optional[LegacyDebtBaseline] = None) -> None:
        self.baseline = baseline or LegacyDebtBaseline()

    def evaluate(
        self,
        records: Sequence[QARecord],
        *,
        failed_unit_identities: Iterable[str] = (),
    ) -> QualityGateResult:
        errors = [r for r in records if r.severity == "error"]
        new_errors = [r for r in errors if is_this_run(r.provenance)]
        carried = [r for r in errors if not is_this_run(r.provenance)]
        unaccepted = [r for r in carried if not self.baseline.accepts(r)]
        failed_count = len(set(failed_unit_identities))
        passed = not new_errors and not unaccepted and failed_count == 0
        return QualityGateResult(
            passed=passed,
            error_count=len(errors),
            failed_unit_count=failed_count,
            new_error_count=len(new_errors),
            carried_error_count=len(carried),
            unaccepted_carried_count=len(unaccepted),
            accepted_debt_count=len(carried) - len(unaccepted),
            baseline_path=str(self.baseline.path) if self.baseline.path else None,
        )

    def require_release(
        self,
        records: Sequence[QARecord],
        *,
        failed_unit_identities: Iterable[str] = (),
    ) -> QualityGateResult:
        result = self.evaluate(records, failed_unit_identities=failed_unit_identities)
        if not result.passed:
            parts = []
            if result.new_error_count:
                parts.append(f"new_errors={result.new_error_count}")
            if result.unaccepted_carried_count:
                parts.append(
                    f"unaccepted_legacy_debt={result.unaccepted_carried_count}"
                    f" (of {result.carried_error_count} carried)"
                )
            if result.failed_unit_count:
                parts.append(f"failed_units={result.failed_unit_count}")
            hint = ""
            if result.unaccepted_carried_count and not result.new_error_count:
                hint = (
                    "; 全部阻断项都来自存量译文。确认无法立即清理时，用 "
                    "`localizer qa-accept-debt <config> <qa-report.json>` 把它们登记进基线"
                    "并提交，之后只有**新增**的存量缺陷会阻断。"
                )
                if not self.baseline.path:
                    # 少了这句，第一个照提示操作的人必然撞 BadParameter 而不知所措。
                    hint += (
                        "（前置条件：项目配置里必须先有 "
                        "`quality_gate.legacy_debt_baseline: <路径>`，"
                        "基线才会进版本库、可 review、可回溯。）"
                    )
            raise QualityGateError(
                "release blocked: " + ", ".join(parts) + hint, result
            )
        return result


class QAReportWriter:
    @staticmethod
    def write(
        directory: Path,
        records: Sequence[QARecord],
        gate: QualityGateResult,
    ) -> Tuple[Path, Path]:
        destination = Path(directory).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        json_path = destination / "qa-report.json"
        csv_path = destination / "qa-report.csv"
        AtomicIO.write_json(
            json_path,
            {
                "summary": gate.as_dict(),
                "issues": [record.to_dict() for record in records],
            },
        )
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow(
            ["severity", "code", "provenance", "relative_path", "stable_identity",
             "message", "details"]
        )
        for record in records:
            writer.writerow(
                [
                    record.severity,
                    record.code,
                    record.provenance,
                    record.relative_path or "",
                    record.stable_identity or "",
                    record.message,
                    str(dict(record.details or {})),
                ]
            )
        AtomicIO.write_text(csv_path, buffer.getvalue())
        return json_path, csv_path
