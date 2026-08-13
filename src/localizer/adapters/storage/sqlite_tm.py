from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator, Optional, Sequence, Tuple


SCHEMA_VERSION = 2

# 面板落表用的固定字段组合。写死是刻意的：任何一处放松都会绕开
# `lookup` / `lookup_reviewed_source` 的其中一条，或者制造下面那个死锁。
HUMAN_REVIEW_FIELDS = {
    "origin": "human",
    "review_state": "reviewed",
    "quality_state": "passed",
    "classification": "native",
    "match_scope": "coordinate_exact",
    "is_formal": True,
    "human_authored": True,
}


class TMGuardError(RuntimeError):
    """写入被 TM 的保护规则拒绝 —— 不是 I/O 故障，重试无意义。"""


class AuthoritySwitchRefused(RuntimeError):
    """权威源切换的前置条件不满足。

    这是一条**不可逆**的边界：切换之后旧入口就再也不许写 `history_tm.json`，
    而 SQLite 成为唯一真相。原实现只检查「两个路径都是存在的文件」——
    `switch_authority(README.md, README.md)` 就能翻牌，M0 那条
    「未完成行为和数据基线，不得切换 TM 权威源」等于没有执行体。
    """


@dataclass(frozen=True)
class HumanWriteResult:
    written: Tuple[str, ...]
    # (stable_identity, 原因)。被治理规则挡下的，不是失败而是**需要人明确决定**。
    guarded: Tuple[Tuple[str, str], ...] = ()


@dataclass(frozen=True)
class TMEntry:
    stable_identity: str
    project_id: str
    adapter_id: str
    relative_path: str
    logical_key: str
    source_text: str
    source_fingerprint: str
    translation: str
    origin: str
    review_state: str
    match_scope: str
    classification: str = "native"
    stage: Optional[int] = None
    run_id: Optional[str] = None
    model: Optional[str] = None
    prompt_hash: Optional[str] = None
    rules_revision: Optional[str] = None
    glossary_revision: Optional[str] = None
    quality_state: str = "candidate"
    is_formal: bool = False
    # 是否由人工产出。§16「机器译文不得静默覆盖人工结果」不能只靠 is_formal 判断：
    # ParaTranz stage 1（已译未审）、2（存疑）、-1（隐藏）都是人工内容但 is_formal=0。
    human_authored: bool = False


class SQLiteTranslationMemory:
    BUSY_TIMEOUT_MS = 15000

    # 唯一一条带闸门的写入 SQL。`upsert`、`upsert_many`、`apply_human_review`
    # 全部走它 —— 那两条 WHERE 是全仓「机器译文永不静默覆盖人工结果」的物理
    # 执行点，任何绕过它的写入路径都等于把这条不变量拆掉。
    _UPSERT_SQL = """
                INSERT INTO tm_entries (
                    stable_identity, project_id, adapter_id, relative_path, logical_key,
                    source_text, source_fingerprint, translation, origin, review_state,
                    match_scope, classification, stage, run_id, model, prompt_hash,
                    rules_revision, glossary_revision, quality_state, is_formal, human_authored,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(stable_identity) DO UPDATE SET
                    source_text=excluded.source_text,
                    source_fingerprint=excluded.source_fingerprint,
                    translation=excluded.translation,
                    origin=excluded.origin,
                    review_state=excluded.review_state,
                    match_scope=excluded.match_scope,
                    classification=excluded.classification,
                    stage=excluded.stage,
                    run_id=excluded.run_id,
                    model=excluded.model,
                    prompt_hash=excluded.prompt_hash,
                    rules_revision=excluded.rules_revision,
                    glossary_revision=excluded.glossary_revision,
                    quality_state=excluded.quality_state,
                    is_formal=excluded.is_formal,
                    human_authored=excluded.human_authored,
                    updated_at=excluded.updated_at
                WHERE (tm_entries.is_formal = 0 OR excluded.is_formal = 1)
                  AND NOT (excluded.origin = 'machine'
                           AND tm_entries.human_authored = 1)
                """

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = Path(path).resolve()
        self.read_only = read_only
        if read_only and self.path.is_file():
            self.connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        elif read_only:
            # A brand-new project can still be preflighted without creating its
            # authoritative/shadow database on disk.
            self.connection = sqlite3.connect(":memory:")
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(str(self.path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        # 显式设置，别吃 Python 默认的 5000ms 再抛 "database is locked"：
        # 面板、CLI build、qa-recheck 都会碰这个库，撞锁是常态而不是异常。
        self.connection.execute(f"PRAGMA busy_timeout = {self.BUSY_TIMEOUT_MS}")
        if not read_only:
            self.connection.execute("PRAGMA journal_mode = WAL")
            self._initialize()
        elif self.path.is_file():
            self._validate_read_only_schema()
        else:
            self._initialize()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "SQLiteTranslationMemory":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def upsert(self, entry: TMEntry) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as connection:
            connection.execute(self._UPSERT_SQL, self._values(entry, now))

    def upsert_many(self, entries: Sequence[TMEntry]) -> Tuple[str, ...]:
        """批量写入，返回**被 WHERE 子句拒绝**的 stable_identity。

        那两条 WHERE 是全仓「机器译文永不静默覆盖人工结果」的物理执行点，
        但它们被拒时是**静默 no-op、零异常**。实测的后果链（既有地雷，不只影响
        面板）：源文改了 → fingerprint 变 → lookup 返回 None → 判 pending →
        机器重译 → staged 行 is_formal=0 → `(is_formal=0 OR excluded.is_formal=1)`
        两边都假 → 行原封不动、run_id 仍是旧值 → 一直跑到 render 之后
        `validate_promotable_run` 才抛「cannot promote entries absent from the
        requested run」，而那时同 run_id 已经不许重试了。

        返回值让调用方能在 render **之前**发现这件事。既有调用点忽略返回值，兼容。
        """
        if not entries:
            return ()
        before = self.rows_for([entry.stable_identity for entry in entries])
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as connection:
            connection.executemany(
                self._UPSERT_SQL,
                [self._values(entry, now) for entry in entries],
            )
        # 读回比对：ON CONFLICT 的 WHERE 不成立时是静默 no-op，
        # 没有任何 rowcount 或异常能告诉调用方这件事。
        after = self.rows_for([entry.stable_identity for entry in entries])
        rejected = []
        for entry in entries:
            row = after.get(entry.stable_identity)
            if row is None:
                rejected.append(entry.stable_identity)
                continue
            if (
                row["translation"] != entry.translation
                or row["source_fingerprint"] != entry.source_fingerprint
                or row["run_id"] != entry.run_id
            ):
                # 行还在，但内容不是这次写的 —— 只有被 WHERE 挡下才会这样。
                previous = before.get(entry.stable_identity)
                if previous is not None and dict(previous) == dict(row):
                    rejected.append(entry.stable_identity)
        return tuple(rejected)

    def rows_for(self, identities: Sequence[str]) -> dict:
        """按 stable_identity 取原始行（不转 TMEntry）—— 供前像与守卫判断。"""
        keys = [str(value) for value in identities if value]
        if not keys:
            return {}
        found = {}
        # SQLite 的变量上限是 999，分批查。
        for start in range(0, len(keys), 500):
            chunk = keys[start : start + 500]
            marks = ",".join("?" for _ in chunk)
            for row in self.connection.execute(
                f"SELECT * FROM tm_entries WHERE stable_identity IN ({marks})", chunk
            ):
                found[row["stable_identity"]] = dict(row)
        return found

    def stale_formal_identities(self, entries: Sequence[TMEntry]) -> Tuple[str, ...]:
        """现有行已是 formal，但源文指纹与本次不符 —— 写入必被 WHERE 挡下。"""
        existing = self.rows_for([entry.stable_identity for entry in entries])
        stale = []
        for entry in entries:
            row = existing.get(entry.stable_identity)
            if row is None or not row["is_formal"] or entry.is_formal:
                continue
            if row["source_fingerprint"] != entry.source_fingerprint:
                stale.append(entry.stable_identity)
        return tuple(stale)

    def apply_human_review(
        self,
        entries: Sequence[TMEntry],
        *,
        allow_remote_override: bool = False,
        reject_guarded: bool = False,
    ) -> HumanWriteResult:
        """写入面板产出的人工定稿译文。

        三道闸，全部 fail-closed：

        1. **字段组合断言**。少一项就会绕开某条 lookup，或者制造死锁 ——
           实测 `is_formal=0` 的人工行会让下一次 release 在 render 之后抛
           「cannot promote entries absent from the requested run」，且同 run_id
           不许重试，整轮白跑。空译文同样拒绝：那不是「清空重译」的正确做法
           （见 `retire_human_entries`）。
        2. **远端保护**。`origin='human', is_formal=1` 的写入会让 upsert 的两条
           WHERE **全部成立**，可以静默覆盖 ParaTranz `stage=9 locked` 的人工
           译文（实测：写前「远端人工定稿」→ 写后「面板改的」，零异常）。
           这与 §7「冲突必须自动保留远端」直接对撞，所以必须先查现有行。
        3. **同事务读回校验**。upsert 被 WHERE 拒绝时是静默 no-op，只看
           rowcount 不够 —— 逐字段比对，不符就 rollback。

        走的是与 `upsert_many` **同一条**带闸门的 SQL，不新开无条件写入路径：
        那两条 WHERE 是全仓唯一的结构性保护，绕过去就等于拆掉它。
        """
        if not entries:
            return HumanWriteResult(())
        for entry in entries:
            self._assert_human_review_shape(entry)

        existing = self.rows_for([entry.stable_identity for entry in entries])
        guarded = []
        writable = []
        for entry in entries:
            row = existing.get(entry.stable_identity)
            reason = self._remote_guard_reason(row) if row else None
            if reason and not allow_remote_override:
                guarded.append((entry.stable_identity, reason))
            else:
                writable.append(entry)
        if guarded and reject_guarded:
            preview = ", ".join(identity for identity, _reason in guarded[:5])
            raise TMGuardError(
                f"refusing partial human write: {len(guarded)} coordinates are guarded; "
                f"first={preview}"
            )
        if not writable:
            return HumanWriteResult((), tuple(guarded))

        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as connection:
            connection.executemany(
                self._UPSERT_SQL, [self._values(entry, now) for entry in writable]
            )
            # rows_for() chunks at 500 variables.  A project-wide artifact
            # attestation can legitimately contain tens of thousands of rows;
            # one giant IN clause exceeds SQLite's variable limit.
            after = self.rows_for([entry.stable_identity for entry in writable])
            for entry in writable:
                row = after.get(entry.stable_identity)
                mismatch = self._readback_mismatch(entry, row)
                if mismatch:
                    raise TMGuardError(
                        f"human review write did not land for "
                        f"{entry.stable_identity}: {mismatch}"
                    )
        return HumanWriteResult(
            tuple(entry.stable_identity for entry in writable), tuple(guarded)
        )

    @staticmethod
    def _assert_human_review_shape(entry: TMEntry) -> None:
        if not entry.translation.strip():
            raise TMGuardError(
                f"refusing to write an empty human translation for "
                f"{entry.stable_identity}; use retire_human_entries() to drop the "
                f"panel's own row and let the next run re-translate it"
            )
        for field, expected in HUMAN_REVIEW_FIELDS.items():
            actual = getattr(entry, field)
            if actual != expected:
                raise TMGuardError(
                    f"human review entry {entry.stable_identity} must have "
                    f"{field}={expected!r}, got {actual!r}"
                )

    @staticmethod
    def _remote_guard_reason(row) -> Optional[str]:
        if row["origin"] == "paratranz" and row["human_authored"]:
            return "远端 ParaTranz 人工译文；§7 要求冲突自动保留远端"
        if row["review_state"] == "locked":
            return "该坐标已被锁定（stage=9）"
        return None

    @staticmethod
    def _readback_mismatch(entry: TMEntry, row) -> Optional[str]:
        if row is None:
            return "row is missing after write"
        checks = {
            "translation": entry.translation,
            "origin": entry.origin,
            "review_state": entry.review_state,
            "quality_state": entry.quality_state,
            "source_fingerprint": entry.source_fingerprint,
            "is_formal": int(entry.is_formal),
            "human_authored": int(entry.human_authored),
        }
        for field, expected in checks.items():
            if row[field] != expected:
                return f"{field}={row[field]!r} (expected {expected!r})"
        return None

    def retire_human_entries(self, identities: Sequence[str]) -> int:
        """删掉**面板自己写的** human 行，让下一次运行重新翻译这些坐标。

        这是「这条我不要了」的唯一出口。刻意不去 UPDATE legacy 或 machine 行：
        改 legacy 影子行会被 `replace_legacy_shadow` 的整段 DELETE 重建抹掉，
        还会把它踢出 `legacy_source_candidates`，从而改变同源收敛的多数派统计、
        波及一批与本次决策毫无关系的坐标。
        """
        keys = [str(value) for value in identities if value]
        if not keys:
            return 0
        marks = ",".join("?" for _ in keys)
        with self.transaction() as connection:
            cursor = connection.execute(
                f"DELETE FROM tm_entries WHERE stable_identity IN ({marks}) "
                f"AND origin = 'human'",
                keys,
            )
            return cursor.rowcount

    def retire_stale_formal_entries(
        self,
        entries: Sequence[TMEntry],
        *,
        expected_identities: Optional[Sequence[str]] = None,
    ) -> int:
        """删除源文指纹已经变化的 formal 行，让当前资源重新接管这些坐标。

        调用方必须先向操作者展示候选并取得明确确认。删除条件在同一事务里
        重新校验 ``is_formal`` 和旧指纹，避免预览后 TM 发生变化时误删新结果。
        """
        candidates = {
            entry.stable_identity: entry.source_fingerprint
            for entry in entries
            if entry.stable_identity and entry.source_fingerprint
        }
        if not candidates:
            return 0
        removed = 0
        with self.transaction() as connection:
            for identity, current_fingerprint in candidates.items():
                cursor = connection.execute(
                    "DELETE FROM tm_entries "
                    "WHERE stable_identity = ? AND is_formal = 1 "
                    "AND source_fingerprint <> ?",
                    (identity, current_fingerprint),
                )
                removed += cursor.rowcount
            if expected_identities is not None:
                expected = {str(value) for value in expected_identities if value}
                if removed != len(expected):
                    raise RuntimeError(
                        f"stale formal candidates changed during retirement: "
                        f"removed {removed}/{len(expected)}; transaction rolled back"
                    )
        return removed

    def restore_rows(self, snapshots: Sequence[dict]) -> int:
        """撤销专用：按前像还原。快照的 `row` 为 None 表示当时这个坐标没有行。

        用 DELETE + INSERT 而不是无条件 UPSERT —— 删干净之后不存在冲突，
        **天然不需要绕过任何闸门**。这正是「撤销要能还原 is_formal=0 的前像」
        与「不许拆掉保护」两个要求的交点。
        """
        restored = 0
        with self.transaction() as connection:
            for snapshot in snapshots:
                identity = snapshot["stable_identity"]
                current = connection.execute(
                    "SELECT origin FROM tm_entries WHERE stable_identity = ?",
                    (identity,),
                ).fetchone()
                if current is not None and current["origin"] != "human":
                    raise TMGuardError(
                        f"refusing to revert {identity}: current row was not written "
                        f"by the review panel (origin={current['origin']!r})"
                    )
                connection.execute(
                    "DELETE FROM tm_entries WHERE stable_identity = ?", (identity,)
                )
                row = snapshot.get("row")
                if row is None:
                    restored += 1
                    continue
                columns = list(row)
                connection.execute(
                    f"INSERT INTO tm_entries ({','.join(columns)}) "
                    f"VALUES ({','.join('?' for _ in columns)})",
                    [row[name] for name in columns],
                )
                restored += 1
        return restored

    def upsert_shadow_many(self, entries: Sequence[TMEntry]) -> None:
        """Refresh legacy shadow rows without modifying native or formal entries."""
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO tm_entries (
                    stable_identity, project_id, adapter_id, relative_path, logical_key,
                    source_text, source_fingerprint, translation, origin, review_state,
                    match_scope, classification, stage, run_id, model, prompt_hash,
                    rules_revision, glossary_revision, quality_state, is_formal, human_authored,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(stable_identity) DO UPDATE SET
                    source_text=excluded.source_text,
                    source_fingerprint=excluded.source_fingerprint,
                    translation=excluded.translation,
                    review_state=excluded.review_state,
                    classification=excluded.classification,
                    quality_state=excluded.quality_state,
                    updated_at=excluded.updated_at
                WHERE tm_entries.origin = 'legacy' AND tm_entries.is_formal = 0
                """,
                [self._values(entry, now) for entry in entries],
            )

    def replace_legacy_shadow(
        self,
        project_id: str,
        entries: Sequence[TMEntry],
        *,
        allow_post_authority: bool = False,
    ) -> None:
        """Make the non-formal legacy shadow reflect the current legacy snapshot exactly.

        切换权威源之后默认拒绝。设计 §12 的阶段表写着「M3 权威源切换后旧入口
        不得直接写权威 TM」，而 R13 只堵住了 `legacy --save-tm` 那个方向 ——
        反方向（拿旧 JSON 重建影子行）一直没有闸门：这个方法会先 DELETE 掉该
        项目全部 legacy 影子行再按旧 JSON 重建，指向一份被清空或损坏的
        `history_tm.json` 就会静默抹掉整批存量译文。

        `allow_post_authority` 留给切换后确需一次性对账的场景，必须由调用方
        显式声明，不给默认值。
        """
        if self.is_authoritative() and not allow_post_authority:
            raise TMGuardError(
                "SQLite TM 已是权威源，拒绝用旧 JSON 重建 legacy 影子行 —— "
                "这会先删掉该项目全部存量影子行再按旧文件重建。"
                "确需切换后对账请显式传 allow_post_authority=True。"
            )
        if any(entry.project_id != project_id or entry.origin != "legacy" for entry in entries):
            raise ValueError("legacy shadow replacement received an entry outside its project")
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM tm_entries WHERE project_id = ? AND origin = 'legacy' AND is_formal = 0",
                (project_id,),
            )
            connection.executemany(
                """
                INSERT INTO tm_entries (
                    stable_identity, project_id, adapter_id, relative_path, logical_key,
                    source_text, source_fingerprint, translation, origin, review_state,
                    match_scope, classification, stage, run_id, model, prompt_hash,
                    rules_revision, glossary_revision, quality_state, is_formal, human_authored,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(stable_identity) DO UPDATE SET
                    source_text=excluded.source_text,
                    source_fingerprint=excluded.source_fingerprint,
                    translation=excluded.translation,
                    review_state=excluded.review_state,
                    classification=excluded.classification,
                    quality_state=excluded.quality_state,
                    updated_at=excluded.updated_at
                WHERE tm_entries.origin = 'legacy' AND tm_entries.is_formal = 0
                """,
                [self._values(entry, now) for entry in entries],
            )

    def lookup(
        self,
        stable_identity: str,
        *,
        source_fingerprint: Optional[str] = None,
        allow_shadow: bool = False,
    ) -> Optional[TMEntry]:
        row = self.connection.execute(
            "SELECT * FROM tm_entries WHERE stable_identity = ?", (stable_identity,)
        ).fetchone()
        if row is None:
            return None
        if source_fingerprint is not None and row["source_fingerprint"] != source_fingerprint:
            return None
        if not row["translation"] or row["quality_state"] == "failed":
            return None
        if not row["is_formal"]:
            if not allow_shadow or row["classification"] != "legacy_clean":
                return None
        if row["review_state"] in {"suspect", "quarantined"}:
            return None
        return self._from_row(row)

    def lookup_reviewed_source(
        self, project_id: str, source_fingerprint: str
    ) -> Optional[TMEntry]:
        rows = self.connection.execute(
            """SELECT * FROM tm_entries
               WHERE project_id = ? AND source_fingerprint = ?
                 AND is_formal = 1 AND quality_state = 'passed'
                 AND translation != ''
                 AND review_state IN ('checked', 'reviewed', 'locked')
               ORDER BY CASE review_state
                    WHEN 'locked' THEN 3 WHEN 'reviewed' THEN 2 ELSE 1 END DESC,
                    updated_at DESC""",
            (project_id, source_fingerprint),
        ).fetchall()
        if not rows:
            return None
        translations = {row["translation"] for row in rows}
        if len(translations) != 1:
            return None
        return self._from_row(rows[0])

    def lookup_legacy_coordinate(
        self,
        *,
        project_id: str,
        adapter_id: str,
        relative_path: str,
        logical_keys: Sequence[str],
        source_fingerprint: str,
    ) -> Tuple[TMEntry, ...]:
        """Return legacy rows compatible with the v5/v6 file-coordinate lookup.

        A source-level conflict makes a legacy row unsafe for global reuse, but it
        does not invalidate the original file/key coordinate.  The application
        layer still re-runs placeholder and target-language QA before accepting a
        returned row.
        """
        normalized_path = relative_path.replace("\\", "/")
        paths = tuple(dict.fromkeys((normalized_path, PurePosixPath(normalized_path).name)))
        keys = tuple(dict.fromkeys(str(value) for value in logical_keys if str(value)))
        if not keys:
            return ()
        path_marks = ",".join("?" for _ in paths)
        key_marks = ",".join("?" for _ in keys)
        rows = self.connection.execute(
            f"""SELECT * FROM tm_entries
                WHERE project_id = ? AND adapter_id = ? AND origin = 'legacy'
                  AND relative_path IN ({path_marks})
                  AND logical_key IN ({key_marks})
                  AND source_fingerprint = ? AND translation != ''
                  AND classification IN ('legacy_clean', 'legacy_suspect')
                  AND review_state != 'quarantined'""",
            (project_id, adapter_id, *paths, *keys, source_fingerprint),
        ).fetchall()
        path_rank = {value: index for index, value in enumerate(paths)}
        key_rank = {value: index for index, value in enumerate(keys)}
        ordered = sorted(
            rows,
            key=lambda row: (
                path_rank.get(row["relative_path"], len(paths)),
                key_rank.get(row["logical_key"], len(keys)),
                row["updated_at"],
            ),
        )
        return tuple(self._from_row(row) for row in ordered)

    def entries_for_project(self, project_id: str) -> Tuple[TMEntry, ...]:
        """项目下的全部行，按坐标稳定排序。

        排序在 SQL 里做而不是 Python 里做，是为了让导出结果与行插入顺序无关 ——
        回滚导出要能逐字节复现，否则「导出两次得到两份不同的文件」会让人
        无法判断到底哪一份对得上当时的库。
        """
        rows = self.connection.execute(
            """SELECT * FROM tm_entries WHERE project_id = ?
               ORDER BY relative_path, logical_key, adapter_id""",
            (project_id,),
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def legacy_source_candidates(self, project_id: str) -> Tuple[TMEntry, ...]:
        """Return per-coordinate-valid legacy candidates for convergence analysis."""
        rows = self.connection.execute(
            """SELECT * FROM tm_entries
               WHERE project_id = ? AND origin = 'legacy' AND translation != ''
                 AND classification IN ('legacy_clean', 'legacy_suspect')
                 AND review_state != 'quarantined'""",
            (project_id,),
        ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def promote_run(self, run_id: str, stable_identities: Iterable[str]) -> int:
        identities = tuple(dict.fromkeys(stable_identities))
        if not identities:
            return 0
        self.validate_promotable_run(run_id, identities)
        placeholders = ",".join("?" for _ in identities)
        with self.transaction() as connection:
            cursor = connection.execute(
                f"""UPDATE tm_entries SET is_formal = 1, updated_at = ?
                    WHERE run_id = ? AND stable_identity IN ({placeholders})""",
                (datetime.now(timezone.utc).isoformat(), run_id, *identities),
            )
        return cursor.rowcount

    def validate_promotable_run(
        self, run_id: str, stable_identities: Iterable[str]
    ) -> None:
        identities = tuple(dict.fromkeys(stable_identities))
        if not identities:
            return
        placeholders = ",".join("?" for _ in identities)
        rows = self.connection.execute(
            f"""SELECT stable_identity, quality_state FROM tm_entries
                WHERE run_id = ? AND stable_identity IN ({placeholders})""",
            (run_id, *identities),
        ).fetchall()
        found = {row["stable_identity"] for row in rows}
        if set(identities) - found:
            raise ValueError("cannot promote entries absent from the requested run")
        if any(row["quality_state"] != "passed" for row in rows):
            raise ValueError("cannot promote entries that did not pass the quality gate")

    def record_sync(self, source_path: Path, source_hash: str, imported_count: int) -> None:
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO legacy_sync(source_path, source_hash, imported_count, synced_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(source_path) DO UPDATE SET
                     source_hash=excluded.source_hash,
                     imported_count=excluded.imported_count,
                     synced_at=excluded.synced_at""",
                (
                    str(Path(source_path).resolve()),
                    source_hash,
                    imported_count,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def last_sync_hash(self, source_path: Path) -> Optional[str]:
        row = self.connection.execute(
            "SELECT source_hash FROM legacy_sync WHERE source_path = ?",
            (str(Path(source_path).resolve()),),
        ).fetchone()
        return row[0] if row else None

    def set_authority(self, enabled: bool) -> None:
        """只能把权威标志置为 false，且**只在它本来就是 false 时**。

        原实现只挡 `enabled=True`。`set_authority(False)` 毫无限制，于是
        `switch_authority` 那句「已经是权威源就拒绝重复切换」两行代码就能绕过：
        置回 false → 再切一次 → `INSERT OR REPLACE` 用新证据覆盖当初的基线
        证据哈希，而那正是唯一能回答「当年凭什么切的」的审计线索。

        要真正退回非权威状态，走 `tm-export-legacy` 导出旧 JSON 再重建库 ——
        那条路会留下文件，不会假装从没切过。
        """
        if enabled:
            raise ValueError(
                "enabling TM authority requires switch_authority with behavior and data baselines"
            )
        if self.is_authoritative():
            raise AuthoritySwitchRefused(
                "SQLite TM 已经是权威源，不能用 set_authority(False) 退回 —— "
                "那会解除单向棘轮并让下一次切换覆盖当初的基线证据哈希。"
                "需要回滚请用 tm-export-legacy 导出旧 JSON 后重建库。"
            )
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES ('authoritative', ?)",
                ("false",),
            )

    def switch_authority(
        self,
        behavior_baseline: Path,
        data_baseline: Path,
        *,
        legacy_source: Path,
        project_id: str,
        expected_legacy_rows: Optional[int] = None,
    ) -> dict:
        """把 SQLite 切成 TM 权威源。**单向**，前置条件逐条校验。

        设计 §12.9 要求的是「短暂冻结旧 TM 写入、执行最终同步、审计和数量核对，
        且行为与数据基线完成」。这里把这句话拆成四条可执行判据：

        1. 两份基线证据必须是 `status: passed` 的结构化报告（`_read_baseline`），
           不是任意一个存在的文件；
        2. 影子同步必须真的跑过 —— `legacy_sync` 至少一行；
        3. **最终**同步：`legacy_sync` 记的哈希必须等于旧 JSON 当前的哈希。
           不等就说明同步之后旧入口又写过，冻结没有成立。`legacy_source`
           因此是**必填**的 —— 它曾经是可选参数，而这是四条里唯一真正验证
           「旧入口写入已冻结」的一条，默认关闭等于整个闸门自愿参加；
        4. 数量核对：同步记录的条数必须能在 TM 里找到对应的存量行。

        已经是权威源时拒绝重复切换：`metadata` 是 INSERT OR REPLACE，重跑一次
        会用新证据悄悄覆盖当初的基线哈希，把审计线索抹掉。
        """
        if self.is_authoritative():
            raise AuthoritySwitchRefused(
                "SQLite TM 已经是权威源；重复切换会覆盖已记录的基线证据哈希。"
            )
        behavior = self._read_baseline(
            behavior_baseline, "behavior_baseline", project_id=project_id
        )
        data = self._read_baseline(data_baseline, "data_baseline", project_id=project_id)

        rows = self.connection.execute(
            "SELECT source_path, source_hash, imported_count FROM legacy_sync"
        ).fetchall()
        if not rows:
            raise AuthoritySwitchRefused(
                "没有任何影子同步记录：切换前必须先完成旧 TM 到影子 SQLite 的最终同步。"
            )
        target = str(Path(legacy_source).resolve())
        if True:
            row = next((r for r in rows if r["source_path"] == target), None)
            if row is None:
                known = ", ".join(sorted(r["source_path"] for r in rows))
                raise AuthoritySwitchRefused(
                    f"{target} 从未同步进影子库；已同步的源是：{known}"
                )
            current = sha256(Path(legacy_source).read_bytes()).hexdigest()
            if current != row["source_hash"]:
                raise AuthoritySwitchRefused(
                    f"{target} 在最后一次影子同步之后又被写过"
                    f"（记录 {row['source_hash'][:12]}，当前 {current[:12]}）。"
                    f"必须冻结旧入口写入并重新同步后才能切换。"
                )
        # 数量核对。原判据是 `expected and legacy_rows == 0` —— 只能检出
        # 「表被整个清空」这一种退化，检不出任何比例的丢失（10000 → 5 照样
        # 放行），而且 `expected == 0` 时整条短路掉。还漏了 project_id 过滤，
        # 同一个库里别的项目的存量行会被算进来。
        legacy_rows = self.connection.execute(
            """SELECT COUNT(*) FROM tm_entries
               WHERE project_id = ? AND classification LIKE 'legacy%'""",
            (project_id,),
        ).fetchone()[0]
        declared = sum(int(row["imported_count"] or 0) for row in rows)
        if declared <= 0:
            raise AuthoritySwitchRefused(
                "数量核对失败：同步记录声明导入 0 条。一条存量都没导入就把 SQLite "
                "切成权威源，等于用一个空库替换掉旧 TM。"
            )
        target = declared if expected_legacy_rows is None else int(expected_legacy_rows)
        if legacy_rows != target:
            raise AuthoritySwitchRefused(
                f"数量核对失败：期望 {target} 条存量行，实际 {legacy_rows} 条"
                f"（同步记录声明共导入 {declared} 条，来自 {len(rows)} 个源）。"
                f" 两种常见成因：(1) 存量行真的丢了；(2) 多个源逐个同步时，"
                f"`replace_legacy_shadow` 每次都会先删掉该项目全部影子行，"
                f"于是 imported_count 的累加值大于库里实际剩下的。"
                f" 核对清楚之后用 expected_legacy_rows 显式声明你认可的数量。"
            )

        evidence = {
            "behavior_baseline_sha256": behavior["digest"],
            "data_baseline_sha256": data["digest"],
            "legacy_sync_sources": str(len(rows)),
            "legacy_rows_at_switch": str(legacy_rows),
            "legacy_rows_declared": str(declared),
            "legacy_rows_reconciled_against": str(target),
            "switched_at": datetime.now(timezone.utc).isoformat(),
        }
        with self.transaction() as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
                (*evidence.items(), ("authoritative", "true")),
            )
        return evidence

    @staticmethod
    def _read_baseline(path: Path, kind: str, *, project_id: str) -> dict:
        """基线证据的最小结构契约。

        故意保持极小：`kind` + `status: passed` + 非空 `summary` + `project_id`
        + 可解析的 `recorded_at`。再多的字段都会变成「为了过闸门而编一份 JSON」
        的负担。

        `project_id` 与 `recorded_at` 是后补的：没有它们，一份基线证据不绑任何
        项目、也不绑任何时刻，「拿另一个项目的基线」和「拿三个月前那份」都能过。
        它们挡不住蓄意伪造 —— 那不是这个闸门的目标；它们挡的是**拿错文件**。
        """
        import json as _json

        target = Path(path).resolve(strict=True)
        if not target.is_file():
            raise AuthoritySwitchRefused(f"{kind} 证据必须是文件：{target}")
        raw = target.read_bytes()
        try:
            payload = _json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise AuthoritySwitchRefused(
                f"{kind} 证据必须是 JSON 报告：{target}（{exc}）"
            ) from exc
        if not isinstance(payload, dict):
            raise AuthoritySwitchRefused(f"{kind} 证据的根必须是对象：{target}")
        if payload.get("kind") != kind:
            raise AuthoritySwitchRefused(
                f"{target} 的 kind 是 {payload.get('kind')!r}，这里需要 {kind!r}"
            )
        if payload.get("status") != "passed":
            raise AuthoritySwitchRefused(
                f"{kind} 证据的 status 是 {payload.get('status')!r}，"
                f"只有 'passed' 才允许切换权威源：{target}"
            )
        summary = payload.get("summary")
        if not isinstance(summary, dict) or not summary:
            raise AuthoritySwitchRefused(
                f"{kind} 证据缺少非空 summary，无法审计：{target}"
            )
        declared_project = payload.get("project_id")
        if declared_project != project_id:
            raise AuthoritySwitchRefused(
                f"{target} 是项目 {declared_project!r} 的基线，本次切换的是 "
                f"{project_id!r}。基线证据必须绑定项目，否则拿错文件不会有人发现。"
            )
        recorded_at = payload.get("recorded_at")
        try:
            datetime.fromisoformat(str(recorded_at).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise AuthoritySwitchRefused(
                f"{kind} 证据的 recorded_at 不是可解析的时间戳"
                f"（{recorded_at!r}）：{target}。没有它就无从判断这份基线有多旧。"
            ) from exc
        return {"digest": sha256(raw).hexdigest(), "payload": payload}

    def is_authoritative(self) -> bool:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = 'authoritative'"
        ).fetchone()
        return bool(row and row[0] == "true")

    def shadow_sync_started(self) -> bool:
        """影子同步是否已经启用。旧入口的写入边界由它决定。"""
        return bool(
            self.connection.execute("SELECT 1 FROM legacy_sync LIMIT 1").fetchone()
        )

    def count_by_classification(self) -> dict:
        rows = self.connection.execute(
            "SELECT classification, COUNT(*) AS count FROM tm_entries GROUP BY classification"
        ).fetchall()
        return {row["classification"]: row["count"] for row in rows}

    def _initialize(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tm_entries (
                    stable_identity TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    adapter_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    logical_key TEXT NOT NULL,
                    source_text TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL,
                    translation TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    review_state TEXT NOT NULL,
                    match_scope TEXT NOT NULL,
                    classification TEXT NOT NULL,
                    stage INTEGER,
                    run_id TEXT,
                    model TEXT,
                    prompt_hash TEXT,
                    rules_revision TEXT,
                    glossary_revision TEXT,
                    quality_state TEXT NOT NULL,
                    is_formal INTEGER NOT NULL DEFAULT 0,
                    human_authored INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tm_source
                    ON tm_entries(project_id, source_fingerprint, review_state, is_formal);
                CREATE INDEX IF NOT EXISTS idx_tm_run ON tm_entries(run_id, quality_state);
                CREATE TABLE IF NOT EXISTS legacy_sync (
                    source_path TEXT PRIMARY KEY,
                    source_hash TEXT NOT NULL,
                    imported_count INTEGER NOT NULL,
                    synced_at TEXT NOT NULL
                );
                """
            )
            row = self.connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self.connection.execute(
                    "INSERT INTO metadata(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                self.connection.execute(
                    "INSERT INTO metadata(key, value) VALUES ('authoritative', 'false')"
                )
            elif int(row[0]) == 1:
                self._migrate_1_to_2()
            elif int(row[0]) != SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported TM schema version {row[0]}; expected {SCHEMA_VERSION}"
                )

    def _validate_read_only_schema(self) -> None:
        try:
            row = self.connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
        except sqlite3.Error as exc:
            raise RuntimeError(f"invalid read-only TM schema: {exc}") from exc
        if row is None or int(row[0]) != SCHEMA_VERSION:
            actual = row[0] if row is not None else "missing"
            raise RuntimeError(
                f"read-only TM schema version {actual}; expected {SCHEMA_VERSION}"
            )

    def _migrate_1_to_2(self) -> None:
        """v1 -> v2：新增 human_authored 并回填。

        v1 的 upsert 只用 is_formal 挡覆盖，而 ParaTranz stage 1/2/-1 的人工内容
        is_formal=0，会被机器译文静默覆盖。这里把已有行的人工署名补上，
        判据与 ParaTranzStagePolicy 一致：origin=paratranz 且 stage 非 0。
        """
        columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(tm_entries)")
        }
        if "human_authored" not in columns:
            self.connection.execute(
                "ALTER TABLE tm_entries "
                "ADD COLUMN human_authored INTEGER NOT NULL DEFAULT 0"
            )
        self.connection.execute(
            "UPDATE tm_entries SET human_authored = 1 "
            "WHERE origin = 'paratranz' AND stage IS NOT NULL AND stage <> 0"
        )
        self.connection.execute(
            "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION),),
        )

    @staticmethod
    def _values(entry: TMEntry, now: str) -> tuple:
        return (
            entry.stable_identity,
            entry.project_id,
            entry.adapter_id,
            entry.relative_path,
            entry.logical_key,
            entry.source_text,
            entry.source_fingerprint,
            entry.translation,
            entry.origin,
            entry.review_state,
            entry.match_scope,
            entry.classification,
            entry.stage,
            entry.run_id,
            entry.model,
            entry.prompt_hash,
            entry.rules_revision,
            entry.glossary_revision,
            entry.quality_state,
            int(entry.is_formal),
            int(entry.human_authored),
            now,
            now,
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> TMEntry:
        return TMEntry(
            stable_identity=row["stable_identity"],
            project_id=row["project_id"],
            adapter_id=row["adapter_id"],
            relative_path=row["relative_path"],
            logical_key=row["logical_key"],
            source_text=row["source_text"],
            source_fingerprint=row["source_fingerprint"],
            translation=row["translation"],
            origin=row["origin"],
            review_state=row["review_state"],
            match_scope=row["match_scope"],
            classification=row["classification"],
            stage=row["stage"],
            run_id=row["run_id"],
            model=row["model"],
            prompt_hash=row["prompt_hash"],
            rules_revision=row["rules_revision"],
            glossary_revision=row["glossary_revision"],
            quality_state=row["quality_state"],
            is_formal=bool(row["is_formal"]),
            human_authored=bool(row["human_authored"]),
        )
