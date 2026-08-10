from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Mapping, Sequence, Tuple

from localizer.infrastructure.atomic_io import AtomicIO


NAMED_PRINTF_RE = re.compile(
    r"%\((?P<name>[^)]+)\)(?P<spec>[-+#0]*\d*(?:\.\d+)?(?P<type>[diouxXeEfFgGcrsa]))?"
)
POSITIONAL_PRINTF_RE = re.compile(r"%(?![%({])[-+#0]*\d*(?:\.\d+)?(?P<type>[diouxXeEfFgGcrsa])")
BRACE_RE = re.compile(r"\{(?P<name>[A-Za-z_][A-Za-z0-9_.\[\]]*|\d+)\}")
TAG_RE = re.compile(r"<(?P<close>/)?(?P<name>[A-Za-z][\w:-]*)(?:\s[^<>]*?)?(?P<self>/)?>")
PERCENT_ESCAPE_RE = re.compile(r"%%")
BARE_PERCENT_RE = re.compile(r"%(?![%({\-+#0\d])")
SUSPICIOUS_RE = re.compile(r"%\s+\(|%\([^)]*$|\{[^{}]*$|^[^{]*\}")


@dataclass(frozen=True)
class PlaceholderObservation:
    file: str
    key: str
    source: str
    translation: str
    source_kinds: Tuple[str, ...]
    target_kinds: Tuple[str, ...]
    anomalies: Tuple[str, ...]


def iter_history_tm(path: Path) -> Iterator[Tuple[str, str, str, str]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("history TM root must be a mapping")
    for logical_file, entries in raw.items():
        if not isinstance(entries, Mapping):
            continue
        for key, value in entries.items():
            if not isinstance(value, Mapping):
                continue
            source = value.get("ru")
            translation = value.get("zh")
            if isinstance(source, str) and isinstance(translation, str):
                yield str(logical_file), str(key), source, translation


def analyze_pair(logical_file: str, key: str, source: str, translation: str) -> PlaceholderObservation:
    source_signature = _signature(source)
    target_signature = _signature(translation)
    anomalies = []
    if source_signature["counts"] != target_signature["counts"]:
        anomalies.append("kind_count_difference")
    if source_signature["names"] != target_signature["names"]:
        anomalies.append("name_difference")
    if source_signature["printf_types"] != target_signature["printf_types"]:
        anomalies.append("printf_type_difference")
    if source_signature["sequence"] != target_signature["sequence"] and Counter(
        source_signature["sequence"]
    ) == Counter(target_signature["sequence"]):
        anomalies.append("order_difference")
    if SUSPICIOUS_RE.search(source) or SUSPICIOUS_RE.search(translation):
        anomalies.append("suspected_broken_expression")
    if not _tags_balanced(source):
        anomalies.append("source_unbalanced_tags")
    if not _tags_balanced(translation):
        anomalies.append("target_unbalanced_tags")
    source_kinds = tuple(sorted(source_signature["counts"]))
    target_kinds = tuple(sorted(target_signature["counts"]))
    if len(source_kinds) > 1:
        anomalies.append("mixed_placeholder_source")
    return PlaceholderObservation(
        logical_file,
        key,
        source,
        translation,
        source_kinds,
        target_kinds,
        tuple(dict.fromkeys(anomalies)),
    )


def analyze_history_tm(tm_path: Path, output_directory: Path) -> dict:
    observations = []
    kind_counts: Counter[str] = Counter()
    anomaly_counts: Counter[str] = Counter()
    sample_groups: Dict[str, List[dict]] = defaultdict(list)
    total = 0
    for logical_file, key, source, translation in iter_history_tm(tm_path):
        total += 1
        observation = analyze_pair(logical_file, key, source, translation)
        if observation.source_kinds:
            observations.append(observation)
        kind_counts.update(observation.source_kinds)
        anomaly_counts.update(observation.anomalies)
        for anomaly in observation.anomalies:
            if len(sample_groups[anomaly]) < 20:
                sample_groups[anomaly].append(asdict(observation))

    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    summary = {
        "schema_version": 1,
        "input": Path(tm_path).name,
        "total_units": total,
        "units_with_placeholders": len(observations),
        "placeholder_kind_counts": dict(sorted(kind_counts.items())),
        "anomaly_counts": dict(sorted(anomaly_counts.items())),
    }
    AtomicIO.write_json(output / "placeholder-summary.json", summary)
    _write_anomalies(output / "placeholder-anomalies.csv", observations)
    _write_samples(output / "placeholder-samples.jsonl", sample_groups)
    AtomicIO.write_text(output / "placeholder-analysis.md", _markdown(summary))
    return summary


def _signature(text: str) -> dict:
    matches = []
    for match in NAMED_PRINTF_RE.finditer(text):
        matches.append(
            (match.start(), "named_printf", match.group("name"), match.group("type") or "untyped")
        )
    for match in POSITIONAL_PRINTF_RE.finditer(text):
        matches.append((match.start(), "positional_printf", "", match.group("type")))
    for match in BRACE_RE.finditer(text):
        matches.append((match.start(), "brace_format", match.group("name"), ""))
    for match in TAG_RE.finditer(text):
        marker = "/" if match.group("close") else ""
        matches.append((match.start(), "tag", marker + match.group("name"), ""))
    for match in PERCENT_ESCAPE_RE.finditer(text):
        matches.append((match.start(), "percent_escape", "", ""))
    for match in BARE_PERCENT_RE.finditer(text):
        matches.append((match.start(), "bare_percent", "", ""))
    matches.sort()
    return {
        "counts": Counter(item[1] for item in matches),
        "names": Counter((item[1], item[2]) for item in matches if item[2]),
        "printf_types": Counter(
            (item[1], item[3]) for item in matches if item[1].endswith("printf")
        ),
        "sequence": tuple((item[1], item[2], item[3]) for item in matches),
    }


def _tags_balanced(text: str) -> bool:
    stack: List[str] = []
    for match in TAG_RE.finditer(text):
        if match.group("self"):
            continue
        name = match.group("name")
        if match.group("close"):
            if not stack or stack.pop() != name:
                return False
        else:
            stack.append(name)
    return not stack


def _write_anomalies(path: Path, observations: Sequence[PlaceholderObservation]) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(("file", "key", "anomaly", "source", "translation"))
    for observation in observations:
        for anomaly in observation.anomalies:
            if anomaly == "mixed_placeholder_source":
                continue
            writer.writerow(
                (observation.file, observation.key, anomaly, observation.source, observation.translation)
            )
    AtomicIO.write_text(path, buffer.getvalue())


def _write_samples(path: Path, groups: Mapping[str, Sequence[dict]]) -> None:
    lines = []
    for anomaly in sorted(groups):
        for sample in groups[anomaly]:
            lines.append(json.dumps({"anomaly": anomaly, **sample}, ensure_ascii=False))
    AtomicIO.write_text(path, "\n".join(lines) + ("\n" if lines else ""))


def _markdown(summary: Mapping[str, object]) -> str:
    kinds = summary["placeholder_kind_counts"]
    anomalies = summary["anomaly_counts"]
    lines = [
        "# 全量占位符分析",
        "",
        f"- 总词条：{summary['total_units']}",
        f"- 含占位符词条：{summary['units_with_placeholders']}",
        "- 数据源：仓库 `history_tm.json`，不依赖游戏文件。",
        "",
        "## 类型分布",
        "",
        "| 类型 | 词条命中数 |",
        "| --- | ---: |",
    ]
    lines.extend(f"| `{name}` | {count} |" for name, count in kinds.items())
    lines.extend(["", "## 异常分布", "", "| 异常 | 数量 |", "| --- | ---: |"])
    lines.extend(f"| `{name}` | {count} |" for name, count in anomalies.items())
    lines.extend(
        [
            "",
            "## 规则归属决策输入",
            "",
            "- 格式语法及结构性占位符应进入 Adapter 通用预设。",
            "- 游戏专名、路径例外和允许残留应进入项目级 `rules.yaml`。",
            "- scope 优先使用 `adapter_id`，必要时叠加路径。",
            "- 歧义或疑似损坏表达式只报告，不猜测修复。",
            "",
        ]
    )
    return "\n".join(lines)
