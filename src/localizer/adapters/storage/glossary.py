from __future__ import annotations

import fnmatch
import json
import re
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import yaml

from localizer.infrastructure.atomic_io import AtomicIO


class GlossaryLoadError(RuntimeError):
    pass


class GlossaryGuardError(RuntimeError):
    pass


@dataclass(frozen=True)
class GlossaryTerm:
    source: str
    target: str
    source_locale: str = "ru-RU"
    target_locale: str = "zh-Hans"
    match_mode: str = "word"
    variants: Tuple[str, ...] = ()
    scope: Optional[str] = None
    # 反向排除：`scope` 是「只在匹配时检查」，表达不了「除了这几处之外都检查」。
    # 多义词必须能反向排除 —— 例如 Серебро 在游戏里既是货币「银币」也是天梯战
    # 段位名「白银」。用 scope 圈定货币语境是做不到的（货币散落在几十个文件里），
    # 只能把段位语境集中的那几个文件排除掉。
    exclude_scope: Tuple[str, ...] = ()
    note: Optional[str] = None
    status: str = "candidate"
    provenance: str = "machine"

    @property
    def key(self) -> Tuple[str, str, str, Optional[str]]:
        return (self.source, self.source_locale, self.target_locale, self.scope)

    def applies_to(self, relative_path: str) -> bool:
        """这条术语该不该在这个资源文件上生效。"""
        if self.scope and not fnmatch.fnmatchcase(relative_path, self.scope):
            return False
        return not any(
            fnmatch.fnmatchcase(relative_path, pattern)
            for pattern in self.exclude_scope
        )

    def matches_source(self, source_text: str) -> bool:
        """源文里出现了这条术语（含变体）吗。"""
        return bool(self.find_source_spans(source_text))

    def find_source_spans(self, source_text: str) -> Tuple[Tuple[int, int, str], ...]:
        """术语在源文里的位置 `(start, end, 命中的写法)`。

        高亮由**服务端**算，前端只渲染区间 —— 前端另写一套正则一定会与
        `matches_source` 漂移，然后出现「高亮了但不报违规」或者反过来。
        两者共用这一个实现，`matches_source` 就是它的布尔投影。
        """
        spans = []
        for form in (self.source, *self.variants):
            if not form:
                continue
            if self.match_mode == "exact":
                if source_text == form:
                    spans.append((0, len(source_text), form))
            elif self.match_mode == "word":
                for match in re.finditer(
                    rf"(?<!\w){re.escape(form)}(?!\w)", source_text
                ):
                    spans.append((match.start(), match.end(), form))
            elif self.match_mode == "substring":
                start = source_text.find(form)
                while start != -1:
                    spans.append((start, start + len(form), form))
                    start = source_text.find(form, start + 1)
            else:
                raise ValueError(f"unsupported glossary match_mode: {self.match_mode}")
        return tuple(sorted(set(spans)))

    def is_violated_by(
        self, source_text: str, translation: str, *, relative_path: str
    ) -> bool:
        """这条译文违反了这条术语吗。

        判据只能有一份。它同时被两处使用，而两处的**后果完全相反**：

        - `LocalBuildPipeline`（构建期）：命中则产出 error，阻断 release；
        - `LegacyTMSynchronizer`（入库期）：命中则判 quarantined，让这条历史译文
          不被坐标回填命中，从而**交给模型重译**。

        两处判据一旦漂移，就会出现「入库时判干净、构建时判违规」这种最坏组合 ——
        坏账被洗成可命中，模型没有重译机会，QA 只能在构建时整包阻断。
        真机实测：修掉多义词误报之后剩下的 684 条术语违规，**全部**来自被入库
        分类器判为 legacy_clean 的历史条目。
        """
        if self.status != "reviewed":
            return False
        if not self.applies_to(relative_path):
            return False
        if not self.matches_source(source_text):
            return False
        return self.target not in translation

    @property
    def human_reviewed(self) -> bool:
        return self.provenance == "human" and self.status == "reviewed"


class GlossaryRepository:
    # 类级锁：审计式写入（读全文 → 拼接 → 整体重写）在并发下会丢事件，
    # 而术语视图的批量操作正是并发来源。跨进程不设防（面板是单进程、
    # 所有写入排进 TaskService 的单线程执行器），这里只挡进程内交错。
    _MAINTENANCE_LOCK = threading.Lock()

    BULK_OPERATIONS = {
        "full_import",
        "full_replace",
        "legacy_migration",
        "external_rebuild",
        "bulk_overwrite",
    }

    def __init__(self, path: Path, *, maintenance_directory: Optional[Path] = None) -> None:
        self.path = Path(path).resolve()
        self.maintenance_directory = (
            Path(maintenance_directory).resolve()
            if maintenance_directory
            else self.path.parent / "glossary_maintenance"
        )

    def load(self) -> Tuple[GlossaryTerm, ...]:
        try:
            text = self.path.read_text(encoding="utf-8")
            if self.path.suffix.lower() == ".json":
                raw = json.loads(text)
            else:
                raw = yaml.safe_load(text)
        except (OSError, UnicodeError, json.JSONDecodeError, yaml.YAMLError) as exc:
            raise GlossaryLoadError(f"cannot load glossary {self.path}: {exc}") from exc
        try:
            terms = tuple(self._decode(raw))
            self._assert_unique(terms)
            return terms
        except (TypeError, ValueError) as exc:
            raise GlossaryLoadError(f"invalid glossary schema {self.path}: {exc}") from exc

    def replace_all(self, terms: Sequence[GlossaryTerm], *, operation: str) -> None:
        if operation not in self.BULK_OPERATIONS:
            raise ValueError(f"operation is not a guarded bulk operation: {operation}")
        current = self.load()
        self._guard_bulk(current, terms)
        self._write(terms)

    def upsert_term(self, term: GlossaryTerm) -> None:
        current = {item.key: item for item in self.load()}
        self._assert_not_protected(current, term.key, replacement=term)
        current[term.key] = term
        self._write(tuple(current.values()))

    def delete_term(self, key: Tuple[str, str, str, Optional[str]]) -> bool:
        current = {item.key: item for item in self.load()}
        self._assert_not_protected(current, key)
        removed = current.pop(key, None)
        if removed is None:
            return False
        self._write(tuple(current.values()))
        return True

    @staticmethod
    def _assert_not_protected(
        current: Mapping[Tuple[str, str, str, Optional[str]], GlossaryTerm],
        key: Tuple[str, str, str, Optional[str]],
        *,
        replacement: Optional[GlossaryTerm] = None,
    ) -> None:
        """G01 的无条件那一半：human + reviewed 的删除/覆盖/降级一律拒绝。

        G01 分两句：80% 比例闸门「仅用于全量导入/替换/迁移」，而「human + reviewed
        任一删除/覆盖/降级均绝对拒绝」是**无条件**的。原来只实现了前一句 ——
        `replace_all` 守住了，`upsert_term`/`delete_term` 完全绕过：实测人工定稿的
        「Боевой пропуск => 战令」能被 delete_term 直接删掉、被 upsert_term 覆写成
        candidate/machine，备份、diff、audit 三条 destructive 要求一条都不触发。

        当 `glossary.auto_discovery: candidate_only` 启用后，机器候选与人工定稿在
        `scope=None` 时 key 可能完全相同，因此所有写路径都必须执行相同保护。

        幂等写入（内容完全一致）不算改动，否则重跑一次同步就会炸。
        """
        existing = current.get(key)
        if existing is None or not existing.human_reviewed:
            return
        if replacement is not None and replacement == existing:
            return
        action = "overwrite or downgrade" if replacement is not None else "delete"
        raise GlossaryGuardError(
            f"refusing to {action} a human+reviewed term: {existing.source}"
            f" => {existing.target}. Use destructive_replace_all(destructive=True,"
            f" reason=...) so a backup, diff and audit record are written."
        )

    def destructive_replace_all(
        self,
        terms: Sequence[GlossaryTerm],
        *,
        destructive: bool,
        reason: str,
    ) -> None:
        if destructive is not True:
            raise GlossaryGuardError("destructive=True is required")
        if not reason.strip():
            raise GlossaryGuardError("a non-empty reason is required")
        self._assert_unique(terms)
        # 备份、diff、审计、写入必须是一个整体：同一时刻两个调用交错的话，
        # 后一个的 load() 会读到前一个写完的内容，diff 变成空的。
        with self._MAINTENANCE_LOCK:
            current = self.load()
            # 微秒精度。秒级时间戳在同一秒内的两次操作会**互相覆盖备份与 diff**：
            # 实测连续两次 destructive_replace_all 之后 .bak 与 diff 各只剩 1 份
            # （audit.jsonl 因为是追加所以幸存，反而让人以为两次都留痕了）。
            # 术语视图一旦支持批量操作，同秒两次是常态而不是边角。
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            self.maintenance_directory.mkdir(parents=True, exist_ok=True)
            backup = self.maintenance_directory / f"{self.path.name}.{timestamp}.bak"
            AtomicIO.write_bytes(backup, self.path.read_bytes())
            diff = self._diff(current, terms)
            AtomicIO.write_json(
                self.maintenance_directory / f"glossary-diff.{timestamp}.json",
                {"reason": reason, **diff},
            )
            audit_path = self.maintenance_directory / "audit.jsonl"
            existing = (
                AtomicIO.read_text(audit_path) if audit_path.exists() else ""
            )
            event = json.dumps(
                {
                    "timestamp": timestamp,
                    "operation": "destructive_replace_all",
                    "reason": reason,
                    "backup": str(backup),
                    **{key: len(value) for key, value in diff.items()},
                },
                ensure_ascii=False,
            )
            AtomicIO.write_text(audit_path, existing + event + "\n")
            self._write(terms)

    def _guard_bulk(
        self, current: Sequence[GlossaryTerm], pending: Sequence[GlossaryTerm]
    ) -> None:
        self._assert_unique(pending)
        current_by_key = {item.key: item for item in current}
        pending_by_key = {item.key: item for item in pending}
        protected_changes = []
        for key, item in current_by_key.items():
            if not item.human_reviewed:
                continue
            replacement = pending_by_key.get(key)
            if replacement is None or replacement != item:
                protected_changes.append(item.source)
        if protected_changes:
            raise GlossaryGuardError(
                "bulk operation would delete, overwrite, or downgrade human+reviewed terms: "
                + ", ".join(sorted(protected_changes))
            )
        if current and len(pending) / len(current) < 0.8:
            raise GlossaryGuardError(
                f"bulk glossary count dropped below 80%: loaded={len(current)}, pending={len(pending)}"
            )

    def _write(self, terms: Sequence[GlossaryTerm]) -> None:
        self._assert_unique(terms)
        ordered = sorted(
            terms,
            key=lambda item: (
                item.source,
                item.source_locale,
                item.target_locale,
                item.scope or "",
            ),
        )
        if self.path.suffix.lower() == ".json":
            payload = {
                item.source: {
                    "zh": item.target,
                    "match": item.match_mode,
                    "status": item.status,
                    "provenance": item.provenance,
                    **({"scope": item.scope} if item.scope else {}),
                    **(
                        {"exclude_scope": list(item.exclude_scope)}
                        if item.exclude_scope
                        else {}
                    ),
                    **({"note": item.note} if item.note else {}),
                    **({"variants": list(item.variants)} if item.variants else {}),
                }
                for item in ordered
            }
            AtomicIO.write_json(self.path, payload)
            return
        payload = {
            "schema_version": 1,
            "terms": [self._encode_term(item) for item in ordered],
        }
        AtomicIO.write_text(
            self.path,
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        )

    @staticmethod
    def _decode(raw: object) -> List[GlossaryTerm]:
        if not isinstance(raw, Mapping):
            raise ValueError("glossary root must be a mapping")
        if "terms" in raw:
            if raw.get("schema_version") != 1 or not isinstance(raw["terms"], list):
                raise ValueError("new glossary format requires schema_version 1 and terms list")
            return [GlossaryRepository._term_from_mapping(item) for item in raw["terms"]]
        terms = []
        for source, value in raw.items():
            if isinstance(value, str):
                terms.append(
                    GlossaryTerm(source=str(source), target=value, status="reviewed", provenance="human")
                )
            elif isinstance(value, Mapping):
                target = value.get("zh", value.get("target"))
                if not isinstance(target, str):
                    raise ValueError(f"term {source!r} has no string target")
                auto = bool(value.get("auto", False))
                terms.append(
                    GlossaryTerm(
                        source=str(source),
                        target=target,
                        match_mode=str(value.get("match", "word")),
                        variants=tuple(value.get("variants", ())),
                        scope=value.get("scope"),
                        exclude_scope=tuple(value.get("exclude_scope", ())),
                        note=value.get("note"),
                        status="candidate" if auto else str(value.get("status", "reviewed")),
                        provenance="machine" if auto else str(value.get("provenance", "human")),
                    )
                )
            else:
                raise ValueError(f"term {source!r} must be a string or mapping")
        return terms

    @staticmethod
    def _term_from_mapping(raw: object) -> GlossaryTerm:
        if not isinstance(raw, Mapping):
            raise ValueError("each term must be a mapping")
        allowed = {
            "source",
            "target",
            "source_locale",
            "target_locale",
            "match_mode",
            "variants",
            "scope",
            "exclude_scope",
            "note",
            "status",
            "provenance",
        }
        extra = set(raw) - allowed
        if extra:
            raise ValueError("unknown term fields: " + ", ".join(sorted(extra)))
        if not isinstance(raw.get("source"), str) or not isinstance(raw.get("target"), str):
            raise ValueError("term source and target must be strings")
        data = dict(raw)
        data["variants"] = tuple(data.get("variants", ()))
        data["exclude_scope"] = tuple(data.get("exclude_scope", ()))
        return GlossaryTerm(**data)

    @staticmethod
    def _encode_term(item: GlossaryTerm) -> dict:
        data = asdict(item)
        data["variants"] = list(item.variants)
        data["exclude_scope"] = list(item.exclude_scope)
        return {key: value for key, value in data.items() if value not in (None, (), [], "")}

    @staticmethod
    def _assert_unique(terms: Sequence[GlossaryTerm]) -> None:
        seen = set()
        duplicates = set()
        for item in terms:
            if item.key in seen:
                duplicates.add(item.key)
            seen.add(item.key)
        if duplicates:
            raise ValueError(f"duplicate glossary identities: {len(duplicates)}")

    @staticmethod
    def _diff(current: Sequence[GlossaryTerm], pending: Sequence[GlossaryTerm]) -> dict:
        before = {item.key: item for item in current}
        after = {item.key: item for item in pending}
        return {
            "removed": [asdict(before[key]) for key in before.keys() - after.keys()],
            "added": [asdict(after[key]) for key in after.keys() - before.keys()],
            "changed": [
                {"before": asdict(before[key]), "after": asdict(after[key])}
                for key in before.keys() & after.keys()
                if before[key] != after[key]
            ],
        }
