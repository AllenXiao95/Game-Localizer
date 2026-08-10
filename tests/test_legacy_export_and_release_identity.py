"""回滚导出与 release 身份前置检查（R17-② / R16-②）。

两条都在「切换 TM 权威源」这条不可逆路径的两端：

- **R17-②**：设计 §12.10 第 10 条要求切换之后「保留导出旧 JSON 的回滚能力」，
  但迁移一直是单向的，`tm-export-legacy` 根本不存在。R13 把权威源切换做成带
  前置条件的单向棘轮之后，这条路径就从「补齐验收项」变成了那个棘轮**唯一的
  逃生口**——没有它，切错了就只能手写 SQL 往回捞。

- **R16-②**：`release` 输出目录已存在的检查只在 `build()` 里，而 `build()` 发生
  在整轮翻译**之后**。往一个已发布的 `run_id` 再跑一次 release，会先把整轮的钱
  烧完，然后在写产物的那一刻才失败。真机一轮是 1,427 条词条、83 万 token。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from localizer.adapters.storage.sqlite_tm import SQLiteTranslationMemory, TMEntry
from localizer.application.local_build import BuildMode, resolve_run_output
from localizer.migrations.legacy_tm import LegacyTMExporter, LegacyTMSynchronizer
from localizer.ports.provider import ProviderResponse, ProviderUsage


def _entry(
    path: str,
    key: str,
    source: str,
    translation: str,
    *,
    classification: str = "legacy_clean",
    adapter_id: str = "gettext",
) -> TMEntry:
    from localizer.domain.translation_unit import TranslationUnit

    unit = TranslationUnit(
        project_id="wot",
        adapter_id=adapter_id,
        relative_path=path,
        logical_key=key,
        source_text=source,
        source_locale="ru-RU",
        target_locale="zh-Hans",
    )
    return TMEntry(
        stable_identity=unit.stable_identity,
        project_id="wot",
        adapter_id=adapter_id,
        relative_path=path,
        logical_key=key,
        source_text=source,
        source_fingerprint=unit.source_fingerprint,
        translation=translation,
        origin="legacy",
        review_state="unreviewed",
        match_scope="coordinate_exact",
        classification=classification,
    )


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name).resolve()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def tm(self) -> SQLiteTranslationMemory:
        return SQLiteTranslationMemory(self.root / "tm.sqlite3")


class ExportShapeTests(_Case):
    def test_the_export_matches_the_legacy_json_shape(self) -> None:
        with self.tm() as tm:
            tm.upsert(_entry("a.mo", "1", "Танк", "坦克"))
            tm.upsert(_entry("a.mo", "2", "Броня", "装甲"))
            tm.upsert(_entry("b.mo", "1", "Экипаж", "乘员"))
            report = LegacyTMExporter(tm, project_id="wot").export(
                self.root / "export.json"
            )
        payload = json.loads((self.root / "export.json").read_text("utf-8"))
        self.assertEqual(
            {
                "a.mo": {
                    "1": {"ru": "Танк", "zh": "坦克"},
                    "2": {"ru": "Броня", "zh": "装甲"},
                },
                "b.mo": {"1": {"ru": "Экипаж", "zh": "乘员"}},
            },
            payload,
        )
        self.assertEqual(3, report.exported)

    def test_the_export_round_trips_through_the_importer(self) -> None:
        """真正的验收：导出的文件必须能被同一个迁移器原样读回来。

        `ru`/`zh` 是旧格式写死的字面量（`multi_i18n_processor_v6.py` 直接读
        `["ru"]`/`["zh"]`），不随项目语言变化。照抄以外的任何"改进"都会让
        旧入口读不了，而回滚的全部意义就是旧入口能读。
        """
        original = {
            "a.mo": {"1": {"ru": "Танк", "zh": "坦克"}},
            "b.mo": {"9": {"ru": "Экипаж", "zh": "乘员"}},
        }
        source = self.root / "history_tm.json"
        source.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
        with self.tm() as tm:
            LegacyTMSynchronizer(
                tm, project_id="wot", source_locale="ru-RU", target_locale="zh-Hans"
            ).sync(source)
            LegacyTMExporter(tm, project_id="wot").export(self.root / "back.json")
        self.assertEqual(
            original, json.loads((self.root / "back.json").read_text("utf-8"))
        )

    def test_the_export_is_byte_stable_across_repeated_runs(self) -> None:
        """导出两次得到两份不同的文件 = 没人能判断哪一份对得上当时的库。"""
        with self.tm() as tm:
            for index in range(20):
                tm.upsert(_entry(f"f{index % 3}.mo", f"k{index}", f"S{index}", f"译{index}"))
            first = LegacyTMExporter(tm, project_id="wot").export(self.root / "one.json")
            second = LegacyTMExporter(tm, project_id="wot").export(self.root / "two.json")
        self.assertEqual(first.export_hash, second.export_hash)
        self.assertEqual(
            (self.root / "one.json").read_bytes(), (self.root / "two.json").read_bytes()
        )


class ExportSafetyTests(_Case):
    def test_it_refuses_to_overwrite_by_default(self) -> None:
        """回滚的目标往往就是旧 TM 本身；用导出结果盖掉它正是要防的事故。"""
        target = self.root / "history_tm.json"
        target.write_text('{"keep": "me"}', encoding="utf-8")
        with self.tm() as tm:
            tm.upsert(_entry("a.mo", "1", "Танк", "坦克"))
            with self.assertRaises(FileExistsError):
                LegacyTMExporter(tm, project_id="wot").export(target)
        self.assertEqual('{"keep": "me"}', target.read_text("utf-8"))

    def test_explicit_overwrite_is_honoured(self) -> None:
        target = self.root / "history_tm.json"
        target.write_text("{}", encoding="utf-8")
        with self.tm() as tm:
            tm.upsert(_entry("a.mo", "1", "Танк", "坦克"))
            LegacyTMExporter(tm, project_id="wot").export(target, overwrite=True)
        self.assertIn("坦克", target.read_text("utf-8"))

    def test_quarantined_rows_are_withheld_and_counted(self) -> None:
        """旧格式没有 classification 维度。导回去的 quarantined 行会变成
        无标记的正常译文，等于把已知坏账重新喂给旧流程。"""
        with self.tm() as tm:
            tm.upsert(_entry("a.mo", "1", "Танк", "坦克"))
            tm.upsert(
                _entry("a.mo", "2", "Броня", "Броня", classification="legacy_quarantined")
            )
            report = LegacyTMExporter(tm, project_id="wot").export(self.root / "x.json")
        payload = json.loads((self.root / "x.json").read_text("utf-8"))
        self.assertEqual({"1"}, set(payload["a.mo"]))
        self.assertEqual(1, report.withheld["legacy_quarantined"])
        self.assertEqual(2, report.total_rows)

    def test_quarantined_rows_can_be_included_explicitly(self) -> None:
        with self.tm() as tm:
            tm.upsert(
                _entry("a.mo", "2", "Броня", "Броня", classification="legacy_quarantined")
            )
            report = LegacyTMExporter(tm, project_id="wot").export(
                self.root / "y.json", include_quarantined=True
            )
        self.assertEqual(1, report.exported)
        self.assertEqual({}, report.withheld)

    def test_empty_translations_never_reach_the_export(self) -> None:
        # ParaTranz 侧的整条 P01–P06 都是「空译文不得覆盖非空」。
        # 回滚导出把空译文写进旧 JSON 就是从后门把那条约束绕过去。
        with self.tm() as tm:
            tm.upsert(_entry("a.mo", "1", "Танк", "   "))
            report = LegacyTMExporter(tm, project_id="wot").export(
                self.root / "z.json", include_quarantined=True
            )
        self.assertEqual(0, report.exported)
        self.assertEqual(1, report.withheld["empty_translation"])

    def test_a_cross_adapter_coordinate_collision_is_reported(self) -> None:
        """旧格式的键是 (relative_path, logical_key)，没有 adapter 维度。

        静默取其一会让回滚后的库悄悄少一批译文，而且没有任何地方能看出来。
        """
        with self.tm() as tm:
            tm.upsert(_entry("a.mo", "1", "Танк", "坦克", adapter_id="gettext"))
            tm.upsert(_entry("a.mo", "1", "Танк", "战车", adapter_id="paratranz_json"))
            report = LegacyTMExporter(tm, project_id="wot").export(self.root / "c.json")
        self.assertEqual(1, report.exported)
        self.assertEqual(1, len(report.collisions))
        collision = report.collisions[0]
        self.assertEqual("a.mo", collision["relative_path"])
        self.assertNotEqual(collision["kept_adapter"], collision["dropped_adapter"])

    def test_counts_are_self_consistent(self) -> None:
        with self.tm() as tm:
            tm.upsert(_entry("a.mo", "1", "Танк", "坦克"))
            tm.upsert(_entry("a.mo", "2", "Броня", ""))
            tm.upsert(
                _entry("a.mo", "3", "Экипаж", "乘员", classification="legacy_quarantined")
            )
            tm.upsert(_entry("a.mo", "1", "Танк", "战车", adapter_id="paratranz_json"))
            report = LegacyTMExporter(tm, project_id="wot").export(self.root / "s.json")
        self.assertEqual(
            report.total_rows,
            report.exported + sum(report.withheld.values()) + len(report.collisions),
        )

    def test_other_projects_are_not_exported(self) -> None:
        with self.tm() as tm:
            tm.upsert(_entry("a.mo", "1", "Танк", "坦克"))
            other = _entry("a.mo", "2", "Броня", "装甲")
            tm.upsert(
                TMEntry(**{**other.__dict__, "project_id": "other", "stable_identity": "x"})
            )
            report = LegacyTMExporter(tm, project_id="wot").export(self.root / "p.json")
        self.assertEqual(1, report.exported)


class CollisionWinnerTests(_Case):
    """同坐标碰撞必须按**权威度**决胜，不是按 adapter_id 字典序。

    旧格式的键是 `(relative_path, logical_key)`，没有 adapter 维度。原来的胜出
    规则完全由 `entries_for_project` 的 `ORDER BY … adapter_id` 决定，方向还恰好
    是反的：gettext 的一条 `origin=legacy, is_formal=0` 机器遗留译文，会挤掉同
    坐标 paratranz 的 `stage=9 locked` 人工锁定译文 —— 回滚文件里留下的是被淘汰
    的那条，而这条路径的全部意义就是「切错了能回来」。
    """

    def _row(self, adapter_id: str, translation: str, **changes) -> TMEntry:
        row = _entry("a.mo", "1", "Танк", translation, adapter_id=adapter_id)
        return TMEntry(**{**row.__dict__, **changes})

    def test_the_locked_human_row_wins_over_a_lexicographically_earlier_adapter(
        self,
    ) -> None:
        with self.tm() as tm:
            tm.upsert(self._row("gettext", "旧的机器遗留译文"))
            tm.upsert(
                self._row(
                    "paratranz_json",
                    "人工锁定的定稿译文",
                    origin="paratranz",
                    review_state="locked",
                    classification="paratranz",
                    stage=9,
                    is_formal=True,
                    human_authored=True,
                )
            )
            report = LegacyTMExporter(tm, project_id="wot").export(self.root / "c.json")
        payload = json.loads((self.root / "c.json").read_text("utf-8"))
        self.assertEqual("人工锁定的定稿译文", payload["a.mo"]["1"]["zh"])
        self.assertEqual(1, len(report.collisions))
        self.assertEqual("paratranz_json", report.collisions[0]["kept_adapter"])

    def test_the_collision_record_says_why(self) -> None:
        """只报「丢了一条」没用 —— 读的人要能判断丢对了没有。"""
        with self.tm() as tm:
            tm.upsert(self._row("gettext", "机器"))
            tm.upsert(
                self._row("paratranz_json", "人工", is_formal=True, human_authored=True)
            )
            report = LegacyTMExporter(tm, project_id="wot").export(self.root / "c.json")
        collision = report.collisions[0]
        self.assertIn("formal", collision["kept_reason"])
        self.assertIn("human", collision["kept_reason"])
        self.assertIn("kept_identity", collision)
        self.assertNotIn("formal", collision["dropped_reason"])

    def test_a_formal_row_beats_a_non_formal_one_regardless_of_adapter(self) -> None:
        # 两个方向都测：字典序在前/在后的 adapter 都不该因为名字而赢。
        for formal_adapter, other in (
            ("android_xml", "paradox_yml"),
            ("paradox_yml", "android_xml"),
        ):
            with self.subTest(formal=formal_adapter):
                with tempfile.TemporaryDirectory() as temp:
                    root = Path(temp).resolve()
                    with SQLiteTranslationMemory(root / "tm.sqlite3") as tm:
                        tm.upsert(self._row(other, "非正式"))
                        tm.upsert(
                            self._row(formal_adapter, "正式", is_formal=True)
                        )
                        report = LegacyTMExporter(tm, project_id="wot").export(
                            root / "c.json"
                        )
                    payload = json.loads((root / "c.json").read_text("utf-8"))
                    self.assertEqual("正式", payload["a.mo"]["1"]["zh"])
                    self.assertEqual(
                        formal_adapter, report.collisions[0]["kept_adapter"]
                    )

    def test_equal_authority_falls_back_to_a_deterministic_tiebreak(self) -> None:
        """完全同权威度时必须仍然确定 —— 导出要逐字节可复现。"""
        digests = set()
        for _ in range(3):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                with SQLiteTranslationMemory(root / "tm.sqlite3") as tm:
                    tm.upsert(self._row("paradox_yml", "B"))
                    tm.upsert(self._row("android_xml", "A"))
                    report = LegacyTMExporter(tm, project_id="wot").export(
                        root / "c.json"
                    )
                digests.add(report.export_hash)
                self.assertEqual("android_xml", report.collisions[0]["kept_adapter"])
        self.assertEqual(1, len(digests))

    def test_the_winner_drives_the_provenance_sidecar(self) -> None:
        """边车必须跟着胜者走，否则会给一条没导出的译文贴人工标记。"""
        with self.tm() as tm:
            tm.upsert(self._row("gettext", "机器"))
            tm.upsert(
                self._row(
                    "paratranz_json",
                    "人工",
                    origin="paratranz",
                    review_state="locked",
                    stage=9,
                    is_formal=True,
                    human_authored=True,
                )
            )
            report = LegacyTMExporter(tm, project_id="wot").export(self.root / "c.json")
        self.assertEqual(1, report.protected_exported)
        sidecar = json.loads(Path(report.provenance_sidecar).read_text("utf-8"))
        self.assertEqual(
            "paratranz", sidecar["entries"]["a.mo"]["1"]["origin"]
        )


class ProvenanceSidecarTests(_Case):
    """回滚导出不得静默摧毁人工审核状态（对抗性审查 CRITICAL）。

    `WITHHELD_CLASSIFICATIONS` 原本只挡 `legacy_quarantined` / `legacy_unknown`，
    而这两个值只出现在 origin='legacy' 的行上。面板人工定稿（classification
    ='native'）与 ParaTranz 锁定行（'paratranz'）一路无标记地导了出去；
    旧格式没有 origin/human_authored 维度，再正向迁移回来时全部变成
    origin='legacy'、human_authored=0 —— `_UPSERT_SQL` 里
    `NOT (excluded.origin='machine' AND tm_entries.human_authored=1)`
    这条**全仓唯一**的物理执行点就此失效，一次普通机器写入就能覆盖人工定稿。

    实测链路：导出 → 新库导入 → 机器写入 → 人工译文被覆盖。
    """

    def _human(self) -> TMEntry:
        row = _entry("a.mo", "1", "Танк", "人工定稿")
        return TMEntry(
            **{
                **row.__dict__,
                "origin": "human",
                "review_state": "reviewed",
                "classification": "native",
                "quality_state": "passed",
                "is_formal": True,
                "human_authored": True,
            }
        )

    def test_protected_rows_are_counted_and_get_a_sidecar(self) -> None:
        with self.tm() as tm:
            tm.upsert(self._human())
            tm.upsert(_entry("a.mo", "2", "Броня", "装甲"))
            report = LegacyTMExporter(tm, project_id="wot").export(self.root / "e.json")
        self.assertEqual(2, report.exported)
        self.assertEqual(1, report.protected_exported)
        self.assertIsNotNone(report.provenance_sidecar)
        self.assertTrue(Path(report.provenance_sidecar).is_file())

    def test_no_sidecar_when_nothing_is_protected(self) -> None:
        """空边车会让人以为「这次导出没有人工内容」，而实际是「没写边车」。"""
        with self.tm() as tm:
            tm.upsert(_entry("a.mo", "1", "Танк", "坦克"))
            report = LegacyTMExporter(tm, project_id="wot").export(self.root / "e.json")
        self.assertEqual(0, report.protected_exported)
        self.assertIsNone(report.provenance_sidecar)

    def test_the_guard_survives_a_full_round_trip(self) -> None:
        """这条是整件事的验收：往返之后机器仍然覆盖不了人工定稿。"""
        identity = self._human().stable_identity
        with self.tm() as tm:
            tm.upsert(self._human())
            LegacyTMExporter(tm, project_id="wot").export(self.root / "back.json")

        second = SQLiteTranslationMemory(self.root / "tm2.sqlite3")
        with second as tm2:
            report = LegacyTMSynchronizer(
                tm2, project_id="wot", source_locale="ru-RU", target_locale="zh-Hans"
            ).sync(self.root / "back.json")
            self.assertEqual(1, report.restored_protected)
            self.assertEqual((), tuple(report.rejected))
            row = tm2.rows_for([identity])[identity]
            self.assertEqual("human", row["origin"])
            self.assertEqual(1, row["human_authored"])
            self.assertEqual(1, row["is_formal"])
            # 机器写入必须被挡下。
            machine = _entry("a.mo", "1", "Танк", "机器覆盖尝试")
            tm2.upsert(TMEntry(**{**machine.__dict__, "origin": "machine"}))
            self.assertEqual(
                "人工定稿", tm2.rows_for([identity])[identity]["translation"]
            )

    def test_a_stale_sidecar_is_refused_rather_than_applied(self) -> None:
        """边车绑在导出那一刻的内容上。

        旧 JSON 被手改过之后再套用边车，等于把「人工已定稿」贴到一份谁也没审过
        的译文上 —— 比丢掉标记更坏。
        """
        with self.tm() as tm:
            tm.upsert(self._human())
            LegacyTMExporter(tm, project_id="wot").export(self.root / "back.json")
        (self.root / "back.json").write_text(
            '{"a.mo": {"1": {"ru": "Танк", "zh": "有人手改了"}}}', encoding="utf-8"
        )
        with SQLiteTranslationMemory(self.root / "tm3.sqlite3") as tm3:
            with self.assertRaises(ValueError) as ctx:
                LegacyTMSynchronizer(
                    tm3, project_id="wot", source_locale="ru-RU", target_locale="zh-Hans"
                ).sync(self.root / "back.json")
            self.assertIn("provenance", str(ctx.exception))

    def test_paratranz_locked_rows_are_protected_too(self) -> None:
        row = _entry("a.mo", "3", "Экипаж", "乘员")
        locked = TMEntry(
            **{
                **row.__dict__,
                "origin": "paratranz",
                "review_state": "locked",
                "classification": "paratranz",
                "stage": 9,
                "is_formal": True,
                "human_authored": True,
            }
        )
        with self.tm() as tm:
            tm.upsert(locked)
            report = LegacyTMExporter(tm, project_id="wot").export(self.root / "p.json")
        self.assertEqual(1, report.protected_exported)

    def test_importing_without_a_sidecar_behaves_exactly_as_before(self) -> None:
        """没有边车时行为必须逐字不变 —— 现网那份 history_tm.json 没有边车。"""
        source = self.root / "plain.json"
        source.write_text(
            '{"a.mo": {"1": {"ru": "Танк", "zh": "坦克"}}}', encoding="utf-8"
        )
        with SQLiteTranslationMemory(self.root / "tm4.sqlite3") as tm:
            report = LegacyTMSynchronizer(
                tm, project_id="wot", source_locale="ru-RU", target_locale="zh-Hans"
            ).sync(source)
            self.assertEqual(0, report.restored_protected)
            identity = _entry("a.mo", "1", "Танк", "坦克").stable_identity
            row = tm.rows_for([identity])[identity]
            self.assertEqual("legacy", row["origin"])
            self.assertEqual(0, row["human_authored"])


class ExportCliTests(_Case):
    def _config(self) -> Path:
        import yaml

        base = yaml.safe_load(
            (ROOT / "projects" / "example" / "project.yaml").read_text("utf-8")
        )
        game = self.root / "game"
        game.mkdir(exist_ok=True)
        base["paths"] = {
            "source": str(game),
            "workspace": str(self.root / "ws"),
            "output": str(self.root / "out"),
        }
        base["glossary"]["file"] = str(ROOT / "tests" / "fixtures" / "scope-glossary.yaml")
        base["rules"]["file"] = str(ROOT / "tests" / "fixtures" / "ru-rules.yaml")
        base["prompt"] = {"template": str(ROOT / "projects" / "example" / "prompt.md")}
        base["tm"]["database"] = str(self.root / "tm.sqlite3")
        base["provider"].pop("tokenizer", None)
        path = self.root / "project.yaml"
        path.write_text(
            yaml.safe_dump(base, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        return path

    def test_the_command_exists_and_refuses_to_clobber(self) -> None:
        from typer.testing import CliRunner

        from localizer.cli.main import app

        config = self._config()
        from localizer.config import load_project_config

        project_id = load_project_config(config).project.id
        with self.tm() as tm:
            row = _entry("a.mo", "1", "Танк", "坦克")
            tm.upsert(TMEntry(**{**row.__dict__, "project_id": project_id}))
        target = self.root / "out.json"
        runner = CliRunner()
        first = runner.invoke(app, ["tm-export-legacy", str(config), str(target)])
        self.assertEqual(0, first.exit_code, first.output)
        self.assertIn("坦克", target.read_text("utf-8"))
        second = runner.invoke(app, ["tm-export-legacy", str(config), str(target)])
        self.assertEqual(2, second.exit_code)

    def test_the_export_does_not_need_write_access_to_the_tm(self) -> None:
        """导出是只读操作。CLI 用 read_only=True 打开库——这条钉死它，
        免得以后有人顺手改成可写，让"回滚"这个动作本身能改坏源数据。"""
        import inspect

        from localizer.cli import main

        source = inspect.getsource(main.tm_export_legacy)
        self.assertIn("read_only=True", source)


class ReleaseIdentityIsCheckedEarlyTests(_Case):
    def test_resolve_run_output_refuses_an_existing_release(self) -> None:
        output = self.root / "out"
        (output / "release" / "r1").mkdir(parents=True)
        with self.assertRaises(FileExistsError):
            resolve_run_output(output, mode=BuildMode.RELEASE, run_id="r1")

    def test_preview_may_be_overwritten(self) -> None:
        output = self.root / "out"
        (output / "preview" / "r1").mkdir(parents=True)
        _root, mode_root = resolve_run_output(
            output, mode=BuildMode.PREVIEW, run_id="r1"
        )
        self.assertEqual(output.resolve() / "preview" / "r1", mode_root)

    def test_a_bad_run_id_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            resolve_run_output(self.root, mode=BuildMode.PREVIEW, run_id="../escape")

    def test_the_run_fails_before_spending_a_single_request(self) -> None:
        """这条就是 R16-② 的全部：Provider 请求数必须是 0。"""
        from tests.test_rebuild_from_run import _CountingProvider, _Project
        from localizer.application.project_runner import ProjectRunner

        project = _Project(self.root)
        (Path(project.config().paths.output) / "release" / "taken").mkdir(parents=True)
        provider = _CountingProvider()
        with self.assertRaises(FileExistsError):
            ProjectRunner(project.config(), provider=provider).run(
                mode=BuildMode.RELEASE, run_id="taken"
            )
        self.assertEqual([], provider.requested)

    def test_a_fresh_release_run_id_still_works(self) -> None:
        from tests.test_rebuild_from_run import _CountingProvider, _Project
        from localizer.application.project_runner import ProjectRunner

        project = _Project(self.root)
        provider = _CountingProvider()
        result = ProjectRunner(project.config(), provider=provider).run(
            mode=BuildMode.RELEASE, run_id="fresh"
        )
        self.assertTrue(result.build.bundle is not None)
        self.assertTrue(provider.requested)


if __name__ == "__main__":
    unittest.main()
