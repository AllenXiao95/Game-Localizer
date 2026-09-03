"""人工决策的**权威**记录：append-only 的决策日志。

为什么 TM 不能当权威：`tm_entries` 没有 author、没有 reason、没有前像、没有
audit —— 它只知道「这条译文现在是什么」，不知道「谁在什么时候把什么改成了
什么、为什么」。而这正是把一个只读观测面板变成可编辑面板时**唯一**必须补上的
东西。TM 是这份日志的**可重放投影**，不是反过来。

日志按月分片。`glossary.py` 的 audit.jsonl 是「读全文 + 拼接 + 整体重写」，
2000 条决策下那会变成每次写入都重写一个几 MB 的文件；这里改成真正的追加。
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# v1 的 JSON 形状允许新增有默认值的字段。旧事件没有 `after` 时仍按空映射读取，
# 因此这次只是向后兼容扩展，不需要把既有月分片整体迁移到 v2。
SCHEMA_VERSION = 1

# 决策事件的动作。draft 不写 TM，只进 ledger；其余都对应一次 TM 写入或术语变更。
ACTIONS = {
    "draft",       # 草稿：操作者填了译文但还没提交
    "commit",      # 落表：写 TM
    "unify",       # 一键统一：为组内每个成员各写一行
    "glossary",    # 术语变更：改译名 / 加 exclude_scope / 降级
    "accept_debt", # 明知有 error 仍放行，必须带 reason
    "retire",      # 删掉面板自己写的行，交回模型重译
    "revert",      # 撤销：按前像还原
    "skip",        # 跳过（不处理）
    "defer",       # 待议
}

EMPTY_REVISION = "0:0000000000000000"


class LogRevisionMismatch(RuntimeError):
    """乐观并发失败：日志在你读取之后被别人追加过。"""


@dataclass(frozen=True)
class ReviewDecisionEvent:
    action: str
    run_id: str
    # 这次决策覆盖的坐标。unify 会有多个。
    targets: Tuple[str, ...] = ()
    translation: Optional[str] = None
    reason: str = ""
    # 完整前像：{stable_identity: 行的全部列 或 None（当时不存在）}。
    # 只存「改了什么」是不够的 —— 撤销要能把行还原成一模一样。
    before: Mapping[str, Optional[Mapping[str, Any]]] = field(default_factory=dict)
    # 新事件同时记录完整后像。Recovery 的 freshness 判据优先直接比较
    # `current == after`；旧事件没有该字段时才退回 ReviewIndex / before-image
    # compatibility proof。它不改变 TM 的 authority：日志仍是权威，TM 仍是投影。
    after: Mapping[str, Optional[Mapping[str, Any]]] = field(default_factory=dict)
    details: Mapping[str, Any] = field(default_factory=dict)
    actor: Mapping[str, str] = field(default_factory=dict)
    decision_id: str = ""
    decided_at: str = ""

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise ValueError(f"unknown review action: {self.action}")
        if not self.decision_id:
            object.__setattr__(self, "decision_id", uuid.uuid4().hex)
        if not self.decided_at:
            # 微秒精度：秒级会让同一秒内的多次决策在排序上不可区分，
            # 而批量操作里同秒几十次是常态。
            object.__setattr__(
                self, "decided_at", datetime.now(timezone.utc).isoformat()
            )

    def to_line(self) -> str:
        payload = {"schema_version": SCHEMA_VERSION, **asdict(self)}
        payload["targets"] = list(self.targets)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ReviewDecisionEvent":
        data = dict(payload)
        version = data.pop("schema_version", None)
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported review decision schema_version {version!r}; "
                f"supported: {SCHEMA_VERSION}"
            )
        data["targets"] = tuple(data.get("targets") or ())
        # 2026-09-03 之前的 v1 事件没有 after；保持原日志可直接读取。
        data.setdefault("after", {})
        return cls(**data)


class ReviewDecisionLog:
    """按月分片的 append-only 日志。

    `path` 是**基准文件名**（例如 `projects/my-game/review/decisions.jsonl`），
    实际分片是同目录下的 `decisions-YYYYMM.jsonl`。
    """

    _LOCK = threading.RLock()

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.directory = self.path.parent
        self.stem = self.path.stem
        self.suffix = self.path.suffix or ".jsonl"

    # ------------------------------------------------------------------ shards

    def shards(self) -> List[Path]:
        if not self.directory.is_dir():
            return []
        pattern = f"{self.stem}-*{self.suffix}"
        return sorted(self.directory.glob(pattern))

    def _shard_for(self, when: str) -> Path:
        month = when[:7].replace("-", "")
        return self.directory / f"{self.stem}-{month}{self.suffix}"

    # ---------------------------------------------------------------- revision

    def revision(self) -> str:
        """当前日志版本。用于乐观并发：提交时带上你读到的那个。

        用「总条数 + 最后一行摘要」而不是整文件哈希 —— 后者在 2000 条决策下
        每次校验都要重读全部分片。
        """
        total = 0
        last = b""
        for shard in self.shards():
            try:
                raw = shard.read_bytes()
            except OSError:
                continue
            lines = [line for line in raw.split(b"\n") if line.strip()]
            total += len(lines)
            if lines:
                last = lines[-1]
        if not total:
            return EMPTY_REVISION
        return f"{total}:{sha256(last).hexdigest()[:16]}"

    # -------------------------------------------------------------------- read

    def read_all(self) -> List[ReviewDecisionEvent]:
        """按**写入顺序**返回全部事件。

        刻意不按 `decided_at` 重排：日志是 append-only，文件顺序就是真相。
        时间戳只有微秒精度，批量操作里同一微秒写入多条是常态，用
        `(decided_at, decision_id)` 排序会让并列事件被随机 uuid 打乱 ——
        而重放顺序直接决定 ledger 的最终状态（先 commit 后 revert 与
        先 revert 后 commit 是两个不同的结果）。分片按月份文件名排序，
        天然是时间序。
        """
        events: List[ReviewDecisionEvent] = []
        for shard in self.shards():
            try:
                text = shard.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                if not line.strip():
                    continue
                events.append(ReviewDecisionEvent.from_payload(json.loads(line)))
        return events

    # ------------------------------------------------------------------ append

    def append(
        self,
        events: Sequence[ReviewDecisionEvent],
        *,
        expected_revision: Optional[str] = None,
    ) -> str:
        """追加事件，返回新的 revision。

        `expected_revision` 不为 None 时做乐观并发校验：不匹配就拒绝整批，
        一条都不写。这样两个人同时在面板上操作时，后提交的那个会被明确挡下、
        看到「日志已变更，请刷新」，而不是静默覆盖对方的判断。
        """
        if not events:
            return self.revision()
        with self._LOCK:
            current = self.revision()
            if expected_revision is not None and expected_revision != current:
                raise LogRevisionMismatch(
                    f"review log changed since you read it "
                    f"(expected {expected_revision}, now {current}); reload and retry"
                )
            self.directory.mkdir(parents=True, exist_ok=True)
            by_shard: Dict[Path, List[str]] = {}
            for event in events:
                by_shard.setdefault(self._shard_for(event.decided_at), []).append(
                    event.to_line()
                )
            for shard, lines in by_shard.items():
                # 真正的追加：不读全文、不重写。O(1) 而不是 O(已有条数)。
                with open(shard, "a", encoding="utf-8", newline="\n") as handle:
                    handle.write("\n".join(lines) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            return self.revision()
