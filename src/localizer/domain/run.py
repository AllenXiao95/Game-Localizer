from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4


class RunStatus(str, Enum):
    CREATED = "created"
    SCANNED = "scanned"
    ANALYZED = "analyzed"
    PLANNED = "planned"
    TRANSLATING = "translating"
    TRANSLATED = "translated"
    QA_PENDING = "qa_pending"
    QUALITY_GATE_PASSED = "quality_gate_passed"
    BUILT = "built"
    PUBLISHED = "published"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    FAILED = "failed"
    BLOCKED_BY_CONFLICT = "blocked_by_conflict"
    BLOCKED_BY_QUALITY_GATE = "blocked_by_quality_gate"


@dataclass
class Run:
    project_id: str
    run_id: str = field(default_factory=lambda: uuid4().hex)
    status: RunStatus = RunStatus.CREATED
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def transition(self, status: RunStatus) -> None:
        self.status = status
