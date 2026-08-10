"""面板落表的 TM 写入 API（W3）。

三件事必须同时成立，缺一就会出真问题：

1. **字段组合写死**。少一项就会绕开某条 lookup，或者制造 release 死锁 ——
   `test_draft_human_row_reproduces_release_deadlock` 正向复现了那个死锁。
2. **远端保护**。`origin='human', is_formal=1` 的写入让 upsert 的两条 WHERE
   **全部成立**，能静默覆盖 ParaTranz `stage=9 locked` 的人工译文
   （实测：写前「远端人工定稿」→ 写后「面板改的」，零异常）。
3. **读回校验**。upsert 被 WHERE 拒绝时是静默 no-op，rowcount 也骗人。

外加一条既有地雷的修复：`upsert_many` 现在报告被拒的 identity，让
「formal 行 + 源文变更」在 render **之前**失败而不是之后。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from localizer.adapters.storage.sqlite_tm import (
    HUMAN_REVIEW_FIELDS,
    SQLiteTranslationMemory,
    TMEntry,
    TMGuardError,
)


def _row(identity: str, **changes) -> TMEntry:
    data = dict(
        stable_identity=identity,
        project_id="wot-ru-zh",
        adapter_id="gettext",
        relative_path="ui.mo",
        logical_key=identity,
        source_text="Привет",
        source_fingerprint="fp-1",
        translation="你好",
        origin="machine",
        review_state="unreviewed",
        match_scope="coordinate_exact",
        quality_state="passed",
        is_formal=False,
    )
    data.update(changes)
    return TMEntry(**data)


def _human(identity: str, translation: str, **changes) -> TMEntry:
    return _row(identity, translation=translation, **{**HUMAN_REVIEW_FIELDS, **changes})


class _TMCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.tm = SQLiteTranslationMemory(Path(self._temp.name) / "tm.sqlite3")

    def tearDown(self) -> None:
        self.tm.close()
        self._temp.cleanup()


class HumanWriteShapeTests(_TMCase):
    def test_written_row_hits_both_lookups(self) -> None:
        self.tm.apply_human_review([_human("only", "人工定稿译文")])
        self.assertEqual("人工定稿译文", self.tm.lookup("only").translation)
        # 全局同源命中要求 is_formal=1 且 review_state ∈ (checked/reviewed/locked)。
        found = self.tm.lookup_reviewed_source("wot-ru-zh", "fp-1")
        self.assertIsNotNone(found)
        self.assertEqual("人工定稿译文", found.translation)

    def test_empty_translation_is_refused(self) -> None:
        with self.assertRaises(TMGuardError) as ctx:
            self.tm.apply_human_review([_human("s", "   ")])
        # 报错必须指出正确的做法，否则人会去改字段组合硬写。
        self.assertIn("retire_human_entries", str(ctx.exception))

    def test_every_required_field_is_asserted(self) -> None:
        for field, expected in HUMAN_REVIEW_FIELDS.items():
            wrong = False if isinstance(expected, bool) else "wrong"
            with self.subTest(field=field):
                with self.assertRaises(TMGuardError) as ctx:
                    self.tm.apply_human_review(
                        [_human("s", "译文", **{field: wrong})]
                    )
                self.assertIn(field, str(ctx.exception))

    def test_draft_human_row_reproduces_release_deadlock(self) -> None:
        """正向复现：`is_formal=0` 的人工行会让 release 死锁。

        这条测试的价值不是守住某个断言，而是**记录为什么字段组合写死**。
        绕过 API 直接写一条 is_formal=0 的人工行，下一轮的机器重译就会被
        upsert 的 WHERE 挡下（两条子句：`is_formal=0 OR excluded.is_formal=1`
        对 draft 行是 True，但 `NOT (excluded.origin='machine' AND
        human_authored=1)` 是 False）→ run_id 保持旧值 →
        `validate_promotable_run` 抛 "cannot promote entries absent from the
        requested run"，而那一步在 render 之后、同 run_id 不许重试。
        """
        self.tm.upsert(
            _row("dead", origin="human", human_authored=True, is_formal=False,
                 review_state="reviewed", translation="草稿译文", run_id=None)
        )
        machine = _row("dead", translation="机器重译", run_id="run-2")
        rejected = self.tm.upsert_many([machine])
        self.assertEqual(("dead",), rejected, "写入应被 WHERE 静默挡下")
        # 用 rows_for 而不是 lookup：`lookup` 对 is_formal=0 的 native 行一律
        # 返回 None（allow_shadow 只放行 classification='legacy_clean'），
        # 所以这条草稿行既命不中、又挡着机器写入 —— 两头不落好，正是它的坏处。
        self.assertEqual("草稿译文", self.tm.rows_for(["dead"])["dead"]["translation"])
        self.assertIsNone(self.tm.lookup("dead", allow_shadow=True))
        with self.assertRaises(ValueError) as ctx:
            self.tm.validate_promotable_run("run-2", ["dead"])
        self.assertIn("absent from the requested run", str(ctx.exception))

        # 走正规路径写的行不会有这个问题：它是 formal，机器写入被拒但
        # run_id 由 promote 管理，且 upsert_many 会当场报告。
        self.tm.retire_human_entries(["dead"])
        self.tm.apply_human_review([_human("dead", "正式人工译文")])
        self.assertTrue(self.tm.lookup("dead").is_formal)


class RemoteProtectionTests(_TMCase):
    def _seed_remote(self, identity: str, **changes) -> None:
        self.tm.upsert(
            _row(
                identity,
                origin="paratranz",
                review_state="locked",
                translation="远端人工定稿",
                human_authored=True,
                is_formal=True,
                **changes,
            )
        )

    def test_paratranz_locked_row_is_guarded(self) -> None:
        self._seed_remote("remote")
        result = self.tm.apply_human_review([_human("remote", "面板改的")])
        self.assertEqual((), result.written)
        self.assertEqual(1, len(result.guarded))
        self.assertEqual("remote", result.guarded[0][0])
        self.assertEqual("远端人工定稿", self.tm.lookup("remote").translation)

    def test_bare_upsert_would_have_overwritten_it(self) -> None:
        """反证：说明上一条测的是闸门，不是「反正写不进去」。

        直接走 upsert_many（即没有这道闸门时的行为），远端 locked 译文会被
        静默覆盖、零异常 —— 这与 §7「冲突必须自动保留远端」直接对撞。
        """
        self._seed_remote("remote")
        self.tm.upsert_many([_human("remote", "面板改的")])
        self.assertEqual("面板改的", self.tm.lookup("remote").translation)

    def test_explicit_override_is_possible_but_must_be_asked_for(self) -> None:
        self._seed_remote("remote")
        result = self.tm.apply_human_review(
            [_human("remote", "面板改的")], allow_remote_override=True
        )
        self.assertEqual(("remote",), result.written)
        self.assertEqual("面板改的", self.tm.lookup("remote").translation)

    def test_locked_row_from_any_origin_is_guarded(self) -> None:
        self.tm.upsert(
            _row("locked", review_state="locked", human_authored=True, is_formal=True)
        )
        result = self.tm.apply_human_review([_human("locked", "面板改的")])
        self.assertEqual((), result.written)

    def test_machine_write_still_cannot_touch_a_panel_row(self) -> None:
        self.tm.apply_human_review([_human("mine", "人工定稿")])
        rejected = self.tm.upsert_many([_row("mine", translation="机器覆盖")])
        self.assertEqual(("mine",), rejected)
        self.assertEqual("人工定稿", self.tm.lookup("mine").translation)


class ReadBackTests(_TMCase):
    def test_silent_no_op_is_turned_into_an_error(self) -> None:
        """把 ON CONFLICT 的 WHERE 改成恒假，精确模拟「被闸门拒绝」。

        这正是真实故障的形态：upsert 静默 no-op、零异常、rowcount 也不可信，
        只有读回比对能发现。
        """
        self.tm.upsert(_row("s", origin="legacy", classification="legacy_clean",
                            translation="旧译文"))
        original = SQLiteTranslationMemory._UPSERT_SQL
        neutered = original.replace(
            "WHERE (tm_entries.is_formal = 0 OR excluded.is_formal = 1)",
            "WHERE 0 AND (tm_entries.is_formal = 0 OR excluded.is_formal = 1)",
        )
        self.assertNotEqual(original, neutered, "没能改到 WHERE，测试是空转的")
        try:
            SQLiteTranslationMemory._UPSERT_SQL = neutered
            with self.assertRaises(TMGuardError) as ctx:
                self.tm.apply_human_review([_human("s", "人工译文")])
            self.assertIn("did not land", str(ctx.exception))
        finally:
            SQLiteTranslationMemory._UPSERT_SQL = original
        # rollback 之后原行必须完好。
        self.assertEqual("旧译文", self.tm.rows_for(["s"])["s"]["translation"])


class StaleFormalTests(_TMCase):
    def test_upsert_many_reports_rejected_identities(self) -> None:
        self.tm.upsert(_row("stale", is_formal=True, run_id="old"))
        rejected = self.tm.upsert_many(
            [_row("stale", source_fingerprint="fp-2", translation="新译文", run_id="new")]
        )
        self.assertEqual(("stale",), rejected)

    def test_stale_formal_identities_finds_them_before_writing(self) -> None:
        self.tm.upsert(_row("stale", is_formal=True, run_id="old"))
        self.tm.upsert(_row("fresh", is_formal=True, run_id="old"))
        stale = self.tm.stale_formal_identities(
            [
                _row("stale", source_fingerprint="fp-2", run_id="new"),
                _row("fresh", source_fingerprint="fp-1", run_id="new"),
            ]
        )
        self.assertEqual(("stale",), stale)

    def test_successful_writes_are_not_reported_as_rejected(self) -> None:
        rejected = self.tm.upsert_many(
            [_row("a", translation="一"), _row("b", translation="二")]
        )
        self.assertEqual((), rejected)


class RetireAndRestoreTests(_TMCase):
    def test_retire_only_deletes_rows_the_panel_wrote(self) -> None:
        self.tm.apply_human_review([_human("mine", "人工")])
        self.tm.upsert(_row("legacy", origin="legacy", classification="legacy_clean"))
        removed = self.tm.retire_human_entries(["mine", "legacy"])
        self.assertEqual(1, removed)
        self.assertEqual({}, self.tm.rows_for(["mine"]))
        self.assertIn("legacy", self.tm.rows_for(["legacy"]))

    def test_restore_puts_the_previous_row_back_verbatim(self) -> None:
        self.tm.upsert(_row("x", origin="legacy", classification="legacy_clean"))
        before = self.tm.rows_for(["x"])["x"]
        self.tm.apply_human_review([_human("x", "人工改写")])
        self.assertEqual("人工改写", self.tm.lookup("x", allow_shadow=True).translation)
        self.tm.restore_rows([{"stable_identity": "x", "row": before}])
        after = self.tm.rows_for(["x"])["x"]
        self.assertEqual(before, after)

    def test_restore_to_absent_deletes_the_row(self) -> None:
        self.tm.apply_human_review([_human("new", "人工")])
        self.tm.restore_rows([{"stable_identity": "new", "row": None}])
        self.assertEqual({}, self.tm.rows_for(["new"]))

    def test_restore_refuses_to_touch_rows_the_panel_did_not_write(self) -> None:
        # 撤销只能撤自己写的。否则它就是一个「还原任意行」的通用后门。
        self.tm.upsert(_row("foreign", origin="legacy", classification="legacy_clean"))
        with self.assertRaises(TMGuardError):
            self.tm.restore_rows([{"stable_identity": "foreign", "row": None}])
        self.assertIn("foreign", self.tm.rows_for(["foreign"]))


class SingleWritePathTests(unittest.TestCase):
    def test_no_upsert_lacks_a_guard_clause(self) -> None:
        """任何 `ON CONFLICT ... DO UPDATE` 都必须带 WHERE 闸门。

        这条不变量比「只能有一条 SQL」更准确：影子同步有自己的闸门
        （`WHERE tm_entries.origin='legacy' AND tm_entries.is_formal=0`），
        它是合法的第二条。**没有 WHERE 的那条才是后门** —— 无条件
        `DO UPDATE` 会废掉「机器译文永不静默覆盖人工结果」这条不变量。
        """
        import re

        source = (SRC / "localizer/adapters/storage/sqlite_tm.py").read_text("utf-8")
        blocks = re.findall(
            r"ON CONFLICT\(stable_identity\) DO UPDATE SET(.*?)(?:\"\"\"|;)",
            source,
            re.S,
        )
        self.assertGreaterEqual(len(blocks), 2, "没找到写入 SQL？")
        for index, block in enumerate(blocks):
            with self.subTest(block=index):
                self.assertIn(
                    "WHERE",
                    block,
                    "出现了无条件的 ON CONFLICT DO UPDATE —— 那是绕过闸门的后门",
                )
        # 人工写入必须复用带闸门的常量，不能自己拼一条。
        self.assertIn("_UPSERT_SQL", source)
        apply_source = re.search(
            r"def apply_human_review.*?(?=\n    def |\n    @staticmethod)", source, re.S
        ).group(0)
        self.assertIn("self._UPSERT_SQL", apply_source)
        self.assertNotIn("ON CONFLICT", apply_source)


if __name__ == "__main__":
    unittest.main()
