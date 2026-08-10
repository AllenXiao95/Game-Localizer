from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from localizer.adapters.storage.glossary import (
    GlossaryGuardError,
    GlossaryLoadError,
    GlossaryRepository,
    GlossaryTerm,
)
from localizer.adapters.storage.sqlite_tm import SQLiteTranslationMemory, TMEntry
from localizer.domain.translation_unit import TranslationUnit
from localizer.migrations.legacy_tm import LegacyTMSynchronizer


def tm_entry(identity: str, **changes: object) -> TMEntry:
    data = {
        "stable_identity": identity,
        "project_id": "wot",
        "adapter_id": "gettext",
        "relative_path": "a/messages.mo",
        "logical_key": identity,
        "source_text": "Hello",
        "source_fingerprint": "source-hash",
        "translation": "你好",
        "origin": "machine",
        "review_state": "unreviewed",
        "match_scope": "coordinate_exact",
        "quality_state": "candidate",
        "is_formal": False,
    }
    data.update(changes)
    return TMEntry(**data)


class SQLiteTMTests(unittest.TestCase):
    def test_read_only_preflight_does_not_create_missing_database(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "missing" / "tm.sqlite3"
            with SQLiteTranslationMemory(database, read_only=True) as tm:
                self.assertIsNone(tm.lookup("missing"))
            self.assertFalse(database.exists())

    def test_shadow_and_failed_entries_never_become_normal_hits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with SQLiteTranslationMemory(Path(temp) / "tm.sqlite3") as tm:
                tm.upsert(tm_entry("shadow", classification="legacy_clean", quality_state="passed"))
                tm.upsert(tm_entry("failed", quality_state="failed", translation="坏结果"))
                self.assertIsNone(tm.lookup("shadow"))
                self.assertEqual("你好", tm.lookup("shadow", allow_shadow=True).translation)
                self.assertIsNone(tm.lookup("failed", allow_shadow=True))

    def test_coordinate_hit_is_rejected_when_source_fingerprint_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with SQLiteTranslationMemory(Path(temp) / "tm.sqlite3") as tm:
                tm.upsert(
                    tm_entry("formal", quality_state="passed", is_formal=True)
                )
                self.assertIsNotNone(
                    tm.lookup("formal", source_fingerprint="source-hash")
                )
                self.assertIsNone(
                    tm.lookup("formal", source_fingerprint="changed-source")
                )

    def test_global_source_hit_requires_review_and_unanimous_translation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with SQLiteTranslationMemory(Path(temp) / "tm.sqlite3") as tm:
                tm.upsert(
                    tm_entry(
                        "reviewed-a",
                        review_state="reviewed",
                        quality_state="passed",
                        is_formal=True,
                    )
                )
                tm.upsert(
                    tm_entry(
                        "machine-a",
                        review_state="unreviewed",
                        translation="机器差异",
                        quality_state="passed",
                        is_formal=True,
                    )
                )
                self.assertEqual(
                    "你好",
                    tm.lookup_reviewed_source("wot", "source-hash").translation,
                )
                tm.upsert(
                    tm_entry(
                        "reviewed-b",
                        review_state="locked",
                        translation="人工冲突",
                        quality_state="passed",
                        is_formal=True,
                    )
                )
                self.assertIsNone(tm.lookup_reviewed_source("wot", "source-hash"))

    def test_shadow_refresh_cannot_overwrite_formal_native_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with SQLiteTranslationMemory(Path(temp) / "tm.sqlite3") as tm:
                tm.upsert(
                    tm_entry(
                        "protected",
                        translation="人工正式译文",
                        origin="human",
                        quality_state="passed",
                        is_formal=True,
                    )
                )
                tm.upsert_shadow_many(
                    [
                        tm_entry(
                            "protected",
                            translation="旧 JSON 译文",
                            origin="legacy",
                            classification="legacy_clean",
                            quality_state="passed",
                        )
                    ]
                )
                self.assertEqual("人工正式译文", tm.lookup("protected").translation)

    def test_only_complete_passed_run_can_be_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with SQLiteTranslationMemory(Path(temp) / "tm.sqlite3") as tm:
                tm.upsert(tm_entry("good", run_id="run-1", quality_state="passed"))
                tm.upsert(tm_entry("bad", run_id="run-1", quality_state="failed"))
                with self.assertRaises(ValueError):
                    tm.promote_run("run-1", ["good", "bad"])
                with self.assertRaises(ValueError):
                    tm.promote_run("run-1", ["missing"])
                self.assertEqual(1, tm.promote_run("run-1", ["good"]))
                self.assertTrue(tm.lookup("good").is_formal)

    def test_authority_is_explicit_and_defaults_off(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with SQLiteTranslationMemory(Path(temp) / "tm.sqlite3") as tm:
                self.assertFalse(tm.is_authoritative())
                with self.assertRaises(ValueError):
                    tm.set_authority(True)
                tm.set_authority(False)
                self.assertFalse(tm.is_authoritative())

    def test_authority_switch_requires_and_records_two_baselines(self) -> None:
        # 证据从「随便两个文件」升级成结构化报告（R13），完整的前置条件
        # 与拒绝路径见 tests/test_tm_authority_gate.py。
        from hashlib import sha256

        from tests.test_tm_authority_gate import write_baseline

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            behavior = write_baseline(root / "behavior.json", "behavior_baseline")
            data = write_baseline(root / "data.json", "data_baseline")
            legacy = root / "history_tm.json"
            legacy.write_text("{}", encoding="utf-8")
            with SQLiteTranslationMemory(root / "tm.sqlite3") as tm:
                tm.upsert(
                    tm_entry(
                        "legacy-1",
                        project_id="wot",
                        origin="legacy",
                        classification="legacy_clean",
                    )
                )
                tm.record_sync(legacy, sha256(legacy.read_bytes()).hexdigest(), 1)
                tm.switch_authority(
                    behavior, data, legacy_source=legacy, project_id="wot"
                )
                self.assertTrue(tm.is_authoritative())
                keys = {
                    row[0]
                    for row in tm.connection.execute(
                        "SELECT key FROM metadata WHERE key LIKE '%baseline_sha256'"
                    )
                }
                self.assertEqual(
                    {"behavior_baseline_sha256", "data_baseline_sha256"}, keys
                )


class LegacyMigrationTests(unittest.TestCase):
    def test_sync_is_read_only_classifies_and_skips_same_hash(self) -> None:
        fixture = {
            "a.mo": {
                "1": {"ru": "Привет", "zh": "你好"},
                "2": {"ru": "Пусто", "zh": ""},
                "3": {"ru": "Остаток", "zh": "仍有 Кириллица"},
                "4": {"ru": "Value %s", "zh": "值"},
            }
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "history_tm.json"
            source.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
            before = source.read_bytes()
            with SQLiteTranslationMemory(root / "tm.sqlite3") as tm:
                report = LegacyTMSynchronizer(
                    tm,
                    project_id="wot",
                    source_locale="ru-RU",
                    target_locale="zh-Hans",
                ).sync(source)
                self.assertEqual(1, report.classifications["legacy_clean"])
                self.assertEqual(3, report.classifications["legacy_quarantined"])
                skipped = LegacyTMSynchronizer(
                    tm,
                    project_id="wot",
                    source_locale="ru-RU",
                    target_locale="zh-Hans",
                ).sync(source, activate_write_guard=True)
                self.assertTrue(skipped.skipped_unchanged)
                guard = Path(str(source) + ".shadow-sync.lock")
                self.assertTrue(guard.exists())
                self.assertIn("shadow synchronization", guard.read_text("utf-8"))
                self.assertFalse(tm.is_authoritative())
            self.assertEqual(before, source.read_bytes())

    def test_resync_removes_deleted_legacy_shadow_but_preserves_formal_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "history_tm.json"
            source.write_text(
                json.dumps(
                    {
                        "a.mo": {
                            "1": {"ru": "Один", "zh": "一"},
                            "2": {"ru": "Два", "zh": "二"},
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with SQLiteTranslationMemory(root / "tm.sqlite3") as tm:
                synchronizer = LegacyTMSynchronizer(
                    tm,
                    project_id="wot",
                    source_locale="ru-RU",
                    target_locale="zh-Hans",
                )
                synchronizer.sync(source)
                self.assertEqual(2, sum(tm.count_by_classification().values()))
                tm.upsert(
                    tm_entry(
                        "formal-extra",
                        project_id="wot",
                        quality_state="passed",
                        is_formal=True,
                    )
                )
                source.write_text(
                    json.dumps(
                        {"a.mo": {"1": {"ru": "Один", "zh": "一"}}},
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                synchronizer.sync(source)
                total = tm.connection.execute("SELECT COUNT(*) FROM tm_entries").fetchone()[0]
                self.assertEqual(2, total)
                self.assertIsNotNone(tm.lookup("formal-extra"))


class GlossaryRepositoryTests(unittest.TestCase):
    @staticmethod
    def terms(count: int) -> tuple[GlossaryTerm, ...]:
        return tuple(
            GlossaryTerm(source=f"term-{index}", target=f"术语-{index}")
            for index in range(count)
        )

    def make_repository(self, root: Path, count: int = 79) -> GlossaryRepository:
        path = root / "glossary.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "terms": [
                        {"source": term.source, "target": term.target}
                        for term in self.terms(count)
                    ],
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return GlossaryRepository(path, maintenance_directory=root / "maintenance")

    def test_legacy_80_percent_boundary_and_failed_write_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = self.make_repository(root)
            before = repository.path.read_bytes()
            with self.assertRaises(GlossaryGuardError):
                repository.replace_all(self.terms(63), operation="full_replace")
            self.assertEqual(before, repository.path.read_bytes())
            repository.replace_all(self.terms(64), operation="full_replace")
            self.assertEqual(64, len(repository.load()))

    def test_human_reviewed_term_is_absolute_protection_on_every_path(self) -> None:
        """G01 的「绝对拒绝」是无条件的，三条写入路径都得守。

        原来这条只断言了 bulk 一条路。`upsert_term`/`delete_term` 完全绕过闸门：
        实测人工定稿条目能被直接删掉、被覆写成 candidate/machine，且备份、diff、
        audit 三条 destructive 要求一条都不触发。
        """
        with tempfile.TemporaryDirectory() as temp:
            repository = self.make_repository(Path(temp), 1)
            protected = GlossaryTerm(
                source="term-0",
                target="人工定稿",
                status="reviewed",
                provenance="human",
            )
            repository.upsert_term(protected)  # 创建保护条目本身是允许的
            with self.assertRaises(GlossaryGuardError):
                repository.replace_all(
                    [GlossaryTerm(source="term-0", target="机器覆盖")],
                    operation="external_rebuild",
                )
            # 覆盖/降级
            with self.assertRaises(GlossaryGuardError):
                repository.upsert_term(
                    GlossaryTerm(
                        source="term-0",
                        target="机器覆盖",
                        status="candidate",
                        provenance="machine",
                    )
                )
            # 删除
            with self.assertRaises(GlossaryGuardError):
                repository.delete_term(protected.key)
            survivor = [t for t in repository.load() if t.source == "term-0"][0]
            self.assertEqual("人工定稿", survivor.target)
            self.assertTrue(survivor.human_reviewed)

    def test_rewriting_a_protected_term_with_identical_content_is_allowed(self) -> None:
        # 幂等写入不算改动，否则同步重跑一次就会炸。
        with tempfile.TemporaryDirectory() as temp:
            repository = self.make_repository(Path(temp), 1)
            protected = GlossaryTerm(
                source="term-0", target="人工定稿", status="reviewed", provenance="human"
            )
            repository.upsert_term(protected)
            repository.upsert_term(protected)
            self.assertEqual(1, len(repository.load()))

    def test_single_transactional_changes_do_not_use_ratio_guard(self) -> None:
        # 删非保护条目不触发 80% 比例闸门（比例闸门只管 bulk），
        # 但 human+reviewed 仍被绝对拒绝 —— 这两件事必须同时成立。
        with tempfile.TemporaryDirectory() as temp:
            repository = self.make_repository(Path(temp), 2)
            key = repository.load()[0].key
            self.assertTrue(repository.delete_term(key))
            self.assertEqual(1, len(repository.load()))

    def test_corrupt_schema_fails_fast_without_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "glossary.yaml"
            path.write_text("schema_version: 1\nterms: not-a-list\n", encoding="utf-8")
            with self.assertRaises(GlossaryLoadError):
                GlossaryRepository(path).load()

    def test_destructive_maintenance_writes_backup_diff_and_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = self.make_repository(root, 2)
            with self.assertRaises(GlossaryGuardError):
                repository.destructive_replace_all([], destructive=False, reason="cleanup")
            repository.destructive_replace_all(
                [], destructive=True, reason="approved data reset"
            )
            maintenance = root / "maintenance"
            self.assertTrue(any(maintenance.glob("*.bak")))
            self.assertTrue(any(maintenance.glob("glossary-diff.*.json")))
            self.assertTrue((maintenance / "audit.jsonl").exists())
            self.assertEqual((), repository.load())


if __name__ == "__main__":
    unittest.main()


class MigrationClassifierChecksTheGlossaryTests(unittest.TestCase):
    """违反已定稿术语的历史译文必须在**入库时**被拦下（评估 R08）。

    机制性成因：入库分类器原本只查空译文、zh==ru、占位符多重集、ValidationRule
    四项，**不查术语表**。于是术语错译被判 `legacy_clean` → 被
    `lookup_legacy_coordinate` 直接命中回填 → **模型永远没有重译机会** →
    QA 在构建期才报违规，而那时它已经是唯一候选，只能整包阻断。

    真机实测（history_tm.json 81709 条）：修掉多义词误报之后剩下的 684 条术语
    违规**全部**来自被判 legacy_clean 的条目；接上术语表后 661 条历史条目转为
    quarantined，从「阻断发布」变成「交给模型重译」。

    必须是 quarantined 而不是 suspect：`lookup_legacy_coordinate` 的 WHERE 里
    `classification IN ('legacy_clean','legacy_suspect')`，suspect 照样会被命中。
    """

    FIXTURE = {
        "menu.mo": {
            "ok": {"ru": "Получить Серебро", "zh": "获得银币"},
            "bad": {"ru": "Потратить Серебро", "zh": "花费贷款"},
        },
        # 段位语境：术语配了 exclude_scope 就不该在这里生效。
        "badge.mo": {
            "rank": {"ru": "Серебро первого сезона", "zh": "白银，第1赛季"},
        },
    }

    TERMS = (
        GlossaryTerm(
            source="Серебро",
            target="银币",
            status="reviewed",
            provenance="human",
            exclude_scope=("badge.mo",),
        ),
    )

    def _sync(self, root: Path, terms):
        source = root / "history_tm.json"
        source.write_text(
            json.dumps(self.FIXTURE, ensure_ascii=False), encoding="utf-8"
        )
        with SQLiteTranslationMemory(root / "tm.sqlite3") as tm:
            report = LegacyTMSynchronizer(
                tm,
                project_id="wot",
                source_locale="ru-RU",
                target_locale="zh-Hans",
                glossary_terms=terms,
            ).sync(source)
            hit = tm.lookup_legacy_coordinate(
                project_id="wot",
                adapter_id="gettext",
                relative_path="menu.mo",
                logical_keys=["bad"],
                source_fingerprint=TranslationUnit(
                    project_id="wot",
                    adapter_id="gettext",
                    relative_path="menu.mo",
                    logical_key="bad",
                    source_text="Потратить Серебро",
                    source_locale="ru-RU",
                    target_locale="zh-Hans",
                ).source_fingerprint,
            )
        return report, hit

    def test_violating_entry_is_quarantined_and_not_hit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            report, hit = self._sync(Path(temp), self.TERMS)
        self.assertEqual(1, report.classifications["legacy_quarantined"])
        self.assertEqual(2, report.classifications["legacy_clean"])
        # 关键断言：坏账不再被坐标回填命中，这条词条会重新进入待翻译队列。
        self.assertEqual((), hit)

    def test_without_the_glossary_the_bad_entry_is_hit_directly(self) -> None:
        # 反证：说明上一条测的是真效果，不是「这条词条本来就命不中」。
        with tempfile.TemporaryDirectory() as temp:
            report, hit = self._sync(Path(temp), ())
        self.assertEqual(3, report.classifications["legacy_clean"])
        self.assertEqual(1, len(hit))
        self.assertEqual("花费贷款", hit[0].translation)

    def test_exclude_scope_is_honoured_at_ingest_too(self) -> None:
        # 段位语境的「白银，第1赛季」不含「银币」，但术语在 badge.mo 被排除，
        # 所以它仍是 clean —— 入库与构建两处的判据必须完全一致。
        with tempfile.TemporaryDirectory() as temp:
            report, _ = self._sync(Path(temp), self.TERMS)
        self.assertEqual(2, report.classifications["legacy_clean"])

    def test_both_stages_share_the_same_judgement(self) -> None:
        import inspect

        from localizer.application import local_build
        from localizer.migrations import legacy_tm

        for module, member in (
            (local_build.LocalBuildPipeline, "_glossary_issues"),
            (legacy_tm.LegacyTMSynchronizer, "_classify_unit"),
        ):
            with self.subTest(where=member):
                self.assertIn(
                    "is_violated_by",
                    inspect.getsource(getattr(module, member)),
                    "两处判据一旦漂移，就会出现「入库判干净、构建判违规」这种最坏组合",
                )
