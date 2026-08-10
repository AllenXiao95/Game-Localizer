"""审查会话状态：哪些决定了、哪些落表了、光标在哪。

它是决策日志的**派生索引**，不是权威 —— `rebuild_from_log()` 能从日志完整重建。
之所以还要它：2000 个决策是跨天的作业，每次打开面板都从头重放全部事件太慢，
而且「草稿」「跳过」「待议」这些状态需要一个可以直接查的形状。

关键：`committed` 是**服务端**标记。分片提交里第 3 块失败时，操作者刷新页面后
必须能分清哪 200 条已落表、哪 100 条没有 —— 否则就得重做 200 次判断。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional
from pathlib import Path

from localizer.application.review_log import ReviewDecisionLog
from localizer.infrastructure.atomic_io import AtomicIO

SCHEMA_VERSION = 1

# 一个目标的处置状态。顺序即优先级：后面的覆盖前面的。
STATES = ("pending", "skipped", "deferred", "draft", "committed", "reverted")


@dataclass
class ReviewLedger:
    path: Path
    items: Dict[str, Dict[str, Any]]
    cursor: Optional[str] = None

    @classmethod
    def load(cls, path: Path) -> "ReviewLedger":
        target = Path(path)
        if not target.is_file():
            return cls(target, {})
        import json

        payload = json.loads(AtomicIO.read_text(target))
        if not isinstance(payload, Mapping):
            raise ValueError(f"review ledger root must be an object: {target}")
        version = payload.get("schema_version")
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"review ledger {target}: unsupported schema_version {version!r}"
            )
        return cls(
            target, dict(payload.get("items") or {}), payload.get("cursor")
        )

    def save(self) -> Path:
        return AtomicIO.write_json(
            self.path,
            {
                "schema_version": SCHEMA_VERSION,
                "cursor": self.cursor,
                "items": self.items,
                "counters": self.counters(),
            },
        )

    def mark(
        self,
        target_id: str,
        state: str,
        *,
        decision_id: Optional[str] = None,
        translation: Optional[str] = None,
        note: str = "",
    ) -> None:
        if state not in STATES:
            raise ValueError(f"unknown ledger state: {state}")
        item = self.items.setdefault(target_id, {})
        item["state"] = state
        if decision_id is not None:
            item["decision_id"] = decision_id
        if translation is not None:
            item["translation"] = translation
        if note:
            item["note"] = note

    def state_of(self, target_id: str) -> str:
        return (self.items.get(target_id) or {}).get("state", "pending")

    def counters(self) -> Dict[str, int]:
        counts = {state: 0 for state in STATES}
        for item in self.items.values():
            state = item.get("state", "pending")
            counts[state] = counts.get(state, 0) + 1
        return counts

    def drafts(self) -> List[str]:
        return sorted(
            key for key, item in self.items.items() if item.get("state") == "draft"
        )

    @classmethod
    def rebuild_from_log(cls, path: Path, log: ReviewDecisionLog) -> "ReviewLedger":
        """从 append-only 日志重建。日志是权威，ledger 随时可以扔掉重来。"""
        ledger = cls(Path(path), {})
        for event in log.read_all():
            for target in event.targets:
                if event.action == "draft":
                    ledger.mark(
                        target, "draft",
                        decision_id=event.decision_id,
                        translation=event.translation,
                    )
                elif event.action in {"commit", "unify", "accept_debt"}:
                    ledger.mark(
                        target, "committed",
                        decision_id=event.decision_id,
                        translation=event.translation,
                    )
                elif event.action == "revert":
                    ledger.mark(target, "reverted", decision_id=event.decision_id)
                elif event.action == "retire":
                    ledger.mark(target, "pending", decision_id=event.decision_id)
                elif event.action == "skip":
                    ledger.mark(target, "skipped", decision_id=event.decision_id)
                elif event.action == "defer":
                    ledger.mark(target, "deferred", decision_id=event.decision_id)
        return ledger
