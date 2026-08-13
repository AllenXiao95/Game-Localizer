"""TM 权威源切换的前置条件与旧入口阶段派生（R13）。

`docs/framework-design.md` 有两条独立的 M0 退出约束，第二条是：

> 未完成行为和数据基线，不得切换 TM 权威源。

§12.9 把它展开成「短暂冻结旧 TM 写入、执行最终同步、审计和数量核对，
且行为与数据基线完成」。修复前这句话**没有任何执行体**：

- `switch_authority(README.md, README.md)` 就能翻牌 —— 唯一的检查是
  「两个路径都是存在的文件」；
- `is_authoritative()` 只在 Manifest 里当一个字符串标签用，不 gate 任何读写；
- `localizer legacy --phase` 默认 `m1_m2`（最宽松），切完权威源之后照样
  `--save-tm` 整库覆盖 `history_tm.json`，`LegacyAccessPolicy` 三条禁令
  一条都不触发。

也就是说 M3 末尾那个「不可逆边界」既拦不住误操作，切换之后也不产生任何约束。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from localizer.adapters.storage.sqlite_tm import (
    AuthoritySwitchRefused,
    SQLiteTranslationMemory,
    TMEntry,
    TMGuardError,
)
from localizer.compat.legacy import (
    LegacyAccessPolicy,
    LegacyPhase,
    phase_for_tm,
    resolve_phase,
)


def write_baseline(
    path: Path,
    kind: str,
    *,
    status: str = "passed",
    project_id: str = "wot",
    recorded_at: str = "2026-08-06T00:00:00+00:00",
) -> Path:
    payload = {
        "schema_version": 1,
        "kind": kind,
        "status": status,
        "project_id": project_id,
        "recorded_at": recorded_at,
        "summary": {"checked": 67, "passed": 67},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _entry(identity: str, classification: str) -> TMEntry:
    return TMEntry(
        stable_identity=identity,
        project_id="wot",
        adapter_id="gettext",
        relative_path="menu.mo",
        logical_key=identity,
        source_text="Танк",
        source_fingerprint="fp",
        translation="坦克",
        origin="legacy",
        review_state="unreviewed",
        match_scope="coordinate_exact",
        classification=classification,
    )


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.behavior = write_baseline(self.root / "behavior.json", "behavior_baseline")
        self.data = write_baseline(self.root / "data.json", "data_baseline")
        self.legacy = self.root / "history_tm.json"
        self.legacy.write_text('{"a.mo": {}}', encoding="utf-8")

    def tearDown(self) -> None:
        self._temp.cleanup()

    def tm(self) -> SQLiteTranslationMemory:
        return SQLiteTranslationMemory(self.root / "tm.sqlite3")

    def legacy_hash(self) -> str:
        return sha256(self.legacy.read_bytes()).hexdigest()

    def synced(self, tm, *, count: int = 1) -> None:
        """把库带到「最终同步已完成」的状态。"""
        for index in range(count):
            tm.upsert(_entry(f"legacy-{index}", "legacy_clean"))
        tm.record_sync(self.legacy, self.legacy_hash(), count)


class EvidenceContractTests(_Case):
    def test_an_arbitrary_file_no_longer_passes(self) -> None:
        """这条就是修复前的整个闸门。"""
        junk = self.root / "README.md"
        junk.write_text("# hello", encoding="utf-8")
        with self.tm() as tm:
            self.synced(tm)
            with self.assertRaises(AuthoritySwitchRefused) as ctx:
                tm.switch_authority(junk, junk, legacy_source=self.legacy, project_id="wot")
            self.assertIn("JSON", str(ctx.exception))
            self.assertFalse(tm.is_authoritative())

    def test_the_two_baselines_are_not_interchangeable(self) -> None:
        # 同一份报告递两次 —— 形式上是「两份证据」，实质只有一份。
        with self.tm() as tm:
            self.synced(tm)
            with self.assertRaises(AuthoritySwitchRefused) as ctx:
                tm.switch_authority(self.behavior, self.behavior, legacy_source=self.legacy, project_id="wot")
            self.assertIn("data_baseline", str(ctx.exception))

    def test_a_failing_baseline_is_refused(self) -> None:
        failing = write_baseline(
            self.root / "bad.json", "data_baseline", status="failed"
        )
        with self.tm() as tm:
            self.synced(tm)
            with self.assertRaises(AuthoritySwitchRefused) as ctx:
                tm.switch_authority(self.behavior, failing, legacy_source=self.legacy, project_id="wot")
            self.assertIn("passed", str(ctx.exception))

    def test_an_empty_summary_is_refused(self) -> None:
        thin = self.root / "thin.json"
        thin.write_text(
            json.dumps({"kind": "data_baseline", "status": "passed", "summary": {}}),
            encoding="utf-8",
        )
        with self.tm() as tm:
            self.synced(tm)
            with self.assertRaises(AuthoritySwitchRefused):
                tm.switch_authority(self.behavior, thin, legacy_source=self.legacy, project_id="wot")


class FreezeAndReconciliationTests(_Case):
    def test_switching_without_any_shadow_sync_is_refused(self) -> None:
        with self.tm() as tm:
            with self.assertRaises(AuthoritySwitchRefused) as ctx:
                tm.switch_authority(self.behavior, self.data, legacy_source=self.legacy, project_id="wot")
            self.assertIn("影子同步", str(ctx.exception))

    def test_a_legacy_write_after_the_final_sync_is_detected(self) -> None:
        """冻结没有成立的唯一可检测证据：文件哈希和同步记录对不上。

        真实场景不是恶意的 —— 有人在切换当天又跑了一次旧流程 `--save-tm`。
        切换之后那次写入就永远丢了，因为旧 JSON 从此不再被读。
        """
        with self.tm() as tm:
            self.synced(tm)
            self.legacy.write_text('{"a.mo": {"1": {}}}', encoding="utf-8")
            with self.assertRaises(AuthoritySwitchRefused) as ctx:
                tm.switch_authority(
                    self.behavior,
                    self.data,
                    legacy_source=self.legacy,
                    project_id="wot",
                )
            self.assertIn("冻结", str(ctx.exception))
            self.assertFalse(tm.is_authoritative())

    def test_a_frozen_and_synced_legacy_source_passes(self) -> None:
        with self.tm() as tm:
            self.synced(tm, count=3)
            evidence = tm.switch_authority(
                self.behavior, self.data, legacy_source=self.legacy, project_id="wot"
            )
            self.assertTrue(tm.is_authoritative())
            self.assertEqual("3", evidence["legacy_rows_at_switch"])
            self.assertIn("switched_at", evidence)

    def test_an_unknown_legacy_source_is_refused(self) -> None:
        other = self.root / "other_tm.json"
        other.write_text("{}", encoding="utf-8")
        with self.tm() as tm:
            self.synced(tm)
            with self.assertRaises(AuthoritySwitchRefused) as ctx:
                tm.switch_authority(self.behavior, self.data, legacy_source=other, project_id="wot")
            self.assertIn("从未同步", str(ctx.exception))

    def test_count_reconciliation_catches_an_empty_table(self) -> None:
        """同步记录说导入了 12 条，表里一条存量行都没有 —— 库被换过或清过。"""
        with self.tm() as tm:
            tm.record_sync(self.legacy, self.legacy_hash(), 12)
            with self.assertRaises(AuthoritySwitchRefused) as ctx:
                tm.switch_authority(self.behavior, self.data, legacy_source=self.legacy, project_id="wot")
            self.assertIn("数量核对", str(ctx.exception))


class OneWayTests(_Case):
    def test_switching_twice_is_refused(self) -> None:
        """metadata 是 INSERT OR REPLACE：重跑会用新证据覆盖当初的基线哈希。

        那正好抹掉唯一能回答「当年是凭什么切的」的审计线索。
        """
        with self.tm() as tm:
            self.synced(tm)
            tm.switch_authority(self.behavior, self.data, legacy_source=self.legacy, project_id="wot")
            first = tm.connection.execute(
                "SELECT value FROM metadata WHERE key='behavior_baseline_sha256'"
            ).fetchone()[0]
            other = write_baseline(self.root / "b2.json", "behavior_baseline")
            other.write_text(
                other.read_text("utf-8").replace("67", "1"), encoding="utf-8"
            )
            with self.assertRaises(AuthoritySwitchRefused):
                tm.switch_authority(other, self.data, legacy_source=self.legacy, project_id="wot")
            self.assertEqual(
                first,
                tm.connection.execute(
                    "SELECT value FROM metadata WHERE key='behavior_baseline_sha256'"
                ).fetchone()[0],
            )

    def test_set_authority_true_is_still_refused(self) -> None:
        with self.tm() as tm:
            with self.assertRaises(ValueError):
                tm.set_authority(True)


class PhaseDerivationTests(_Case):
    def test_phase_tracks_the_database_state(self) -> None:
        with self.tm() as tm:
            self.assertEqual(LegacyPhase.M1_M2, phase_for_tm(tm))
            self.synced(tm)
            self.assertEqual(LegacyPhase.M3_SHADOW, phase_for_tm(tm))
            tm.switch_authority(self.behavior, self.data, legacy_source=self.legacy, project_id="wot")
            self.assertEqual(LegacyPhase.SQLITE_AUTHORITY, phase_for_tm(tm))

    def test_the_derived_phase_actually_blocks_save_tm(self) -> None:
        """这是 R13 的另一半：切换之后旧入口必须真的写不了。

        修复前 `--phase` 默认 `m1_m2`，`may_write_history_json` 恒为 True。
        """
        with self.tm() as tm:
            self.synced(tm)
            tm.switch_authority(self.behavior, self.data, legacy_source=self.legacy, project_id="wot")
            policy = LegacyAccessPolicy(phase_for_tm(tm))
            self.assertFalse(policy.may_write_history_json)
            self.assertFalse(policy.may_read_history_json)
            with self.assertRaises(RuntimeError):
                policy.validate_arguments(["--save-tm"])
            # argparse 前缀消歧的缩写同样挡住。
            with self.assertRaises(RuntimeError):
                policy.validate_arguments(["--save-t"])

    def test_an_explicit_phase_may_tighten_but_not_loosen(self) -> None:
        self.assertEqual(
            LegacyPhase.RETIRED,
            resolve_phase(LegacyPhase.M3_SHADOW, LegacyPhase.RETIRED),
        )
        self.assertEqual(
            LegacyPhase.M3_SHADOW, resolve_phase(LegacyPhase.M3_SHADOW, None)
        )
        with self.assertRaises(RuntimeError) as ctx:
            resolve_phase(LegacyPhase.SQLITE_AUTHORITY, LegacyPhase.M1_M2)
        self.assertIn("不允许降级", str(ctx.exception))

    def test_same_phase_is_allowed(self) -> None:
        self.assertEqual(
            LegacyPhase.M3_SHADOW,
            resolve_phase(LegacyPhase.M3_SHADOW, LegacyPhase.M3_SHADOW),
        )


class CliContractTests(_Case):
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

    def test_cli_refuses_and_exits_nonzero(self) -> None:
        from typer.testing import CliRunner

        from localizer.cli.main import app

        config = self._config()
        junk = self.root / "junk.md"
        junk.write_text("nope", encoding="utf-8")
        result = CliRunner().invoke(
            app,
            [
                "tm-switch-authority",
                str(config),
                "--behavior-baseline",
                str(junk),
                "--data-baseline",
                str(junk),
            ],
        )
        self.assertEqual(2, result.exit_code)
        with self.tm() as tm:
            self.assertFalse(tm.is_authoritative())

    def test_cli_legacy_refuses_to_downgrade_the_phase(self) -> None:
        from typer.testing import CliRunner

        from localizer.cli.main import app

        config = self._config()
        with self.tm() as tm:
            self.synced(tm)
            tm.switch_authority(self.behavior, self.data, legacy_source=self.legacy, project_id="wot")
        result = CliRunner().invoke(
            app,
            [
                "legacy",
                "--config",
                str(config),
                "--phase",
                "m1_m2",
                "--repository-root",
                str(self.root),
                "--save-tm",
            ],
        )
        self.assertNotEqual(0, result.exit_code)
        self.assertIn("不允许降级", result.output)


if __name__ == "__main__":
    unittest.main()


class BypassesAreClosedTests(_Case):
    """对抗性审查在这个闸门上找到 4 个绕过点，全部是「参数可选」或「反方向没堵」。

    共同形状值得记住：闸门本身写对了，但**进入闸门是自愿的**。
    一个默认值、一个 `Optional`、一个没人查的反向写入路径，就足以让
    「不可逆边界」退化成一句注释。
    """

    def _switched(self, tm) -> None:
        self.synced(tm)
        tm.switch_authority(self.behavior, self.data, legacy_source=self.legacy, project_id="wot")

    def test_legacy_source_is_mandatory(self) -> None:
        """它曾经是 Optional，而这是四条判据里唯一真正验证「旧入口已冻结」的一条。

        省掉一个可选参数，「切换当天有人又跑了一次旧流程 --save-tm」就完全检不
        出来，而切换不可逆、那次写入从此永远丢失。
        """
        with self.tm() as tm:
            self.synced(tm)
            with self.assertRaises(TypeError):
                tm.switch_authority(self.behavior, self.data)

    def test_set_authority_false_cannot_unwind_the_ratchet(self) -> None:
        """两行代码曾经就能解除单向棘轮并覆盖当初的基线证据哈希。

        `switch_authority` 拒绝重复切换的理由是「INSERT OR REPLACE 会抹掉唯一
        能回答『当年凭什么切的』的审计线索」；而 `set_authority(False)` 只挡
        `enabled=True`，置回 false 之后那个拒绝条件立刻为假。
        """
        with self.tm() as tm:
            self._switched(tm)
            evidence = tm.connection.execute(
                "SELECT value FROM metadata WHERE key='behavior_baseline_sha256'"
            ).fetchone()[0]
            with self.assertRaises(AuthoritySwitchRefused) as ctx:
                tm.set_authority(False)
            self.assertIn("单向棘轮", str(ctx.exception))
            self.assertTrue(tm.is_authoritative())
            self.assertEqual(
                evidence,
                tm.connection.execute(
                    "SELECT value FROM metadata WHERE key='behavior_baseline_sha256'"
                ).fetchone()[0],
            )

    def test_set_authority_false_still_works_before_the_switch(self) -> None:
        # 没切过的库照旧允许显式置 false —— 这条路本来就存在，不该顺手关掉。
        with self.tm() as tm:
            tm.set_authority(False)
            self.assertFalse(tm.is_authoritative())

    def test_legacy_shadow_cannot_be_rebuilt_after_the_switch(self) -> None:
        """R13 堵了 `legacy --save-tm` 方向，反方向一直没堵。

        `replace_legacy_shadow` 会先 DELETE 掉该项目全部 legacy 影子行再按旧
        JSON 重建。切换之后指向一份被清空/损坏的 history_tm.json 跑一次
        `tm-sync-legacy`，就会静默抹掉整批存量译文 —— 而设计 §12 的阶段表
        明写「M3 权威源切换后旧入口不得直接写权威 TM」。
        """
        with self.tm() as tm:
            self._switched(tm)
            before = tm.connection.execute(
                "SELECT COUNT(*) FROM tm_entries WHERE origin='legacy'"
            ).fetchone()[0]
            self.assertEqual(1, before)
            with self.assertRaises(TMGuardError) as ctx:
                tm.replace_legacy_shadow("wot", [])
            self.assertIn("权威源", str(ctx.exception))
            self.assertEqual(
                before,
                tm.connection.execute(
                    "SELECT COUNT(*) FROM tm_entries WHERE origin='legacy'"
                ).fetchone()[0],
            )

    def test_a_deliberate_post_authority_reconciliation_is_still_possible(self) -> None:
        """闸门是为了让人**明确决定**，不是为了把路堵死。"""
        with self.tm() as tm:
            self._switched(tm)
            tm.replace_legacy_shadow("wot", [], allow_post_authority=True)
            self.assertEqual(
                0,
                tm.connection.execute(
                    "SELECT COUNT(*) FROM tm_entries WHERE origin='legacy'"
                ).fetchone()[0],
            )

    def test_shadow_rebuild_is_unrestricted_before_the_switch(self) -> None:
        # 影子同步阶段本来就该能反复重建，别把正常流程也拦了。
        with self.tm() as tm:
            self.synced(tm)
            tm.replace_legacy_shadow("wot", [])
            self.assertFalse(tm.is_authoritative())


class LegacyCliFailsClosedTests(_Case):
    """`legacy --config` 曾经可选，不传就回落到最宽松的 M1_M2。

    也就是说刚堵上的洞又留了一个「不写这个参数就当没切过」的口子。
    「不知道库处于哪个阶段」和「库处于最宽松阶段」是两件完全不同的事。
    """

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

    def test_omitting_config_is_now_an_error_not_a_downgrade(self) -> None:
        from click import unstyle
        from typer.testing import CliRunner

        from localizer.cli.main import app

        result = CliRunner().invoke(
            app, ["legacy", "--repository-root", str(self.root), "--save-tm"]
        )
        self.assertNotEqual(0, result.exit_code)
        self.assertIn("--config", unstyle(result.output))

    def test_switch_authority_cli_requires_the_legacy_tm(self) -> None:
        from click import unstyle
        from typer.testing import CliRunner

        from localizer.cli.main import app

        config = self._config()
        result = CliRunner().invoke(
            app,
            [
                "tm-switch-authority",
                str(config),
                "--behavior-baseline",
                str(self.behavior),
                "--data-baseline",
                str(self.data),
            ],
        )
        self.assertNotEqual(0, result.exit_code)
        self.assertIn("--legacy-tm", unstyle(result.output))


class ReconciliationIsNotDecorativeTests(_Case):
    """数量核对原本只能检出「表被整个清空」这一种退化。

    `if expected and legacy_rows == 0` —— 10000 → 5 照样放行；`expected == 0`
    时整条判据短路掉；而且没有 project_id 过滤，同一个库里别的项目的存量行
    会被算进来。而这是一条**不可逆**切换的最后一道数量闸门。
    """

    def _switch(self, tm, **kwargs):
        return tm.switch_authority(
            self.behavior,
            self.data,
            legacy_source=self.legacy,
            project_id="wot",
            **kwargs,
        )

    def test_a_partial_loss_is_now_detected(self) -> None:
        """10000 声明、5 条实际 —— 修复前这是绿灯。"""
        with self.tm() as tm:
            self.synced(tm, count=5)
            # 把声明数改成 10000，模拟「多源同步互相覆盖」或「行真的丢了」。
            tm.record_sync(self.legacy, self.legacy_hash(), 10000)
            with self.assertRaises(AuthoritySwitchRefused) as ctx:
                self._switch(tm)
            message = str(ctx.exception)
            self.assertIn("数量核对失败", message)
            self.assertIn("10000", message)
            self.assertIn("5", message)

    def test_declaring_zero_imports_is_refused(self) -> None:
        """一条存量都没导入就翻牌，等于用一个空库替换掉旧 TM。"""
        with self.tm() as tm:
            tm.record_sync(self.legacy, self.legacy_hash(), 0)
            with self.assertRaises(AuthoritySwitchRefused) as ctx:
                self._switch(tm)
            self.assertIn("0 条", str(ctx.exception))

    def test_another_projects_rows_do_not_count(self) -> None:
        """漏了 project_id 过滤时，别的项目的存量行会把核对糊弄过去。"""
        with self.tm() as tm:
            self.synced(tm, count=1)
            other = _entry("other-1", "legacy_clean")
            tm.upsert(TMEntry(**{**other.__dict__, "project_id": "another"}))
            tm.record_sync(self.legacy, self.legacy_hash(), 2)
            with self.assertRaises(AuthoritySwitchRefused):
                self._switch(tm)

    def test_an_explicit_reconciled_count_is_honoured(self) -> None:
        """核对清楚之后必须能显式声明 —— 闸门是为了让人明确决定，不是堵死。"""
        with self.tm() as tm:
            self.synced(tm, count=5)
            tm.record_sync(self.legacy, self.legacy_hash(), 10000)
            evidence = self._switch(tm, expected_legacy_rows=5)
            self.assertTrue(tm.is_authoritative())
            self.assertEqual("5", evidence["legacy_rows_at_switch"])
            self.assertEqual("10000", evidence["legacy_rows_declared"])
            self.assertEqual("5", evidence["legacy_rows_reconciled_against"])

    def test_an_exact_match_passes(self) -> None:
        with self.tm() as tm:
            self.synced(tm, count=3)
            self._switch(tm)
            self.assertTrue(tm.is_authoritative())


class BaselineEvidenceIsBoundTests(_Case):
    """基线证据必须绑项目和时刻。

    最小契约挡不住蓄意伪造 —— 那不是这个闸门的目标；它挡的是**拿错文件**：
    拿另一个项目的基线、拿三个月前那份，原来都能过。
    """

    def test_a_baseline_from_another_project_is_refused(self) -> None:
        stray = write_baseline(
            self.root / "stray.json", "behavior_baseline", project_id="another-game"
        )
        with self.tm() as tm:
            self.synced(tm)
            with self.assertRaises(AuthoritySwitchRefused) as ctx:
                tm.switch_authority(
                    stray, self.data, legacy_source=self.legacy, project_id="wot"
                )
            self.assertIn("another-game", str(ctx.exception))

    def test_a_baseline_without_a_timestamp_is_refused(self) -> None:
        thin = self.root / "thin.json"
        thin.write_text(
            json.dumps(
                {
                    "kind": "data_baseline",
                    "status": "passed",
                    "project_id": "wot",
                    "summary": {"x": 1},
                }
            ),
            encoding="utf-8",
        )
        with self.tm() as tm:
            self.synced(tm)
            with self.assertRaises(AuthoritySwitchRefused) as ctx:
                tm.switch_authority(
                    self.behavior, thin, legacy_source=self.legacy, project_id="wot"
                )
            self.assertIn("recorded_at", str(ctx.exception))

    def test_an_unparsable_timestamp_is_refused(self) -> None:
        bad = write_baseline(
            self.root / "bad.json", "data_baseline", recorded_at="上周三"
        )
        with self.tm() as tm:
            self.synced(tm)
            with self.assertRaises(AuthoritySwitchRefused):
                tm.switch_authority(
                    self.behavior, bad, legacy_source=self.legacy, project_id="wot"
                )
