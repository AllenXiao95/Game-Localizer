"""存量债不得永久阻塞增量打包，但也不得被静默放行。

真机验证（docs/preview-validation-20260802.md §4/§6）实测：853 个 error 里
**849 个来自历史 TM 命中，只有 2 个来自本次机器新译**。QualityGate 原本对全量
error 零容忍，意味着只要历史 TM 里还有一条坏账，任何 release 都发不出去。

这组测试锁住修复后的两条性质：
1. 本次运行自己产出的译文上的 error —— 永远零容忍，不可记账；
2. 搬运过来的存量 error —— 只有登记在基线里的才放行，新增的照样阻断（棘轮）。
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

from localizer.application.quality_gate import (
    PROVENANCE_THIS_RUN,
    LegacyDebtBaseline,
    QARecord,
    QAReportWriter,
    QualityGate,
    QualityGateError,
)


def _error(identity: str, code: str, provenance: str) -> QARecord:
    return QARecord(
        code=code,
        severity="error",
        message=f"{code} on {identity}",
        stable_identity=identity,
        relative_path="ui.mo",
        details={},
        provenance=provenance,
    )


class GateSplitsByProvenanceTests(unittest.TestCase):
    def test_carried_errors_alone_still_block_without_a_baseline(self) -> None:
        # 默认行为不变：没有基线时全量零容忍，不会悄悄放松。
        gate = QualityGate()
        records = [_error("u1", "glossary_violation", "legacy_coordinate_exact")]
        result = gate.evaluate(records)
        self.assertFalse(result.passed)
        self.assertEqual(0, result.new_error_count)
        self.assertEqual(1, result.carried_error_count)
        self.assertEqual(1, result.unaccepted_carried_count)

    def test_accepted_carried_errors_stop_blocking(self) -> None:
        records = [
            _error("u1", "glossary_violation", "legacy_coordinate_exact"),
            _error("u2", "untranslated", "embedded"),
        ]
        baseline = LegacyDebtBaseline([r.debt_key for r in records])
        result = QualityGate(baseline).require_release(records)
        self.assertTrue(result.passed)
        self.assertEqual(2, result.accepted_debt_count)
        self.assertEqual(0, result.unaccepted_carried_count)

    def test_this_run_errors_are_never_excused_by_the_baseline(self) -> None:
        fresh = _error("u9", "placeholder_mismatch", PROVENANCE_THIS_RUN)
        # 即便把它写进基线也不行 —— 本次自己产出的缺陷必须真修。
        baseline = LegacyDebtBaseline([fresh.debt_key])
        with self.assertRaises(QualityGateError) as ctx:
            QualityGate(baseline).require_release([fresh])
        self.assertEqual(1, ctx.exception.result.new_error_count)
        self.assertIn("new_errors=1", str(ctx.exception))

    def test_new_legacy_debt_beyond_the_baseline_blocks(self) -> None:
        known = _error("u1", "glossary_violation", "legacy_coordinate_exact")
        baseline = LegacyDebtBaseline([known.debt_key])
        appeared = _error("u2", "glossary_violation", "legacy_coordinate_exact")
        with self.assertRaises(QualityGateError) as ctx:
            QualityGate(baseline).require_release([known, appeared])
        # 棘轮：债只能减不能增。
        self.assertEqual(1, ctx.exception.result.unaccepted_carried_count)
        self.assertEqual(1, ctx.exception.result.accepted_debt_count)

    def test_blocked_by_legacy_only_message_points_at_the_baseline_command(self) -> None:
        with self.assertRaises(QualityGateError) as ctx:
            QualityGate().require_release(
                [_error("u1", "untranslated", "legacy_coordinate_exact")]
            )
        self.assertIn("qa-accept-debt", str(ctx.exception))

    def test_debt_key_is_per_identity_and_code(self) -> None:
        # 同一条目的不同问题各算一笔债；清掉一类不会顺带放行另一类。
        a = _error("u1", "untranslated", "embedded")
        b = _error("u1", "glossary_violation", "embedded")
        baseline = LegacyDebtBaseline([a.debt_key])
        self.assertTrue(baseline.accepts(a))
        self.assertFalse(baseline.accepts(b))


class BaselineFileTests(unittest.TestCase):
    def test_writer_records_only_carried_errors(self) -> None:
        records = [
            _error("u1", "glossary_violation", "legacy_coordinate_exact"),
            _error("u2", "placeholder_mismatch", PROVENANCE_THIS_RUN),
            QARecord("same_source_inconsistency", "warning", "m", "u3", "ui.mo", {},
                     "legacy_coordinate_exact"),
        ]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "baseline.json"
            LegacyDebtBaseline().write(path, records, note="首次记账")
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(1, payload["accepted_total"], "只收 carried 的 error")
        self.assertEqual({"glossary_violation": 1}, payload["by_code"])
        self.assertEqual("首次记账", payload["note"])
        # 本次新增的 error 与 warning 都不进基线。
        self.assertTrue(all("placeholder_mismatch" not in k for k in payload["accepted"]))

    def test_round_trip(self) -> None:
        records = [_error("u1", "untranslated", "embedded")]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "baseline.json"
            LegacyDebtBaseline().write(path, records)
            loaded = LegacyDebtBaseline.load(path)
        self.assertTrue(loaded.accepts(records[0]))
        self.assertEqual(str(path), str(loaded.path))

    def test_missing_baseline_file_is_an_empty_ratchet_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            loaded = LegacyDebtBaseline.load(Path(temp) / "absent.json")
        self.assertEqual(set(), loaded.accepted)

    def test_corrupt_baseline_fails_loudly(self) -> None:
        # 基线读不出来必须报错，不能当成空基线继续 —— 那等于悄悄恢复零容忍，
        # 或者反过来在别的实现里等于悄悄全放行。两种静默都不可接受。
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "baseline.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ValueError):
                LegacyDebtBaseline.load(path)

    def test_wrong_schema_version_fails_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "baseline.json"
            path.write_text(json.dumps({"schema_version": 99, "accepted": []}),
                            encoding="utf-8")
            with self.assertRaises(ValueError):
                LegacyDebtBaseline.load(path)


class ReportCarriesProvenanceTests(unittest.TestCase):
    def test_report_columns_include_provenance(self) -> None:
        records = [
            _error("u1", "glossary_violation", "legacy_coordinate_exact"),
            _error("u2", "untranslated", PROVENANCE_THIS_RUN),
        ]
        gate = QualityGate().evaluate(records)
        with tempfile.TemporaryDirectory() as temp:
            json_path, csv_path = QAReportWriter.write(Path(temp), records, gate)
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            csv_text = csv_path.read_text(encoding="utf-8")
        # 报告必须带来源，否则 qa-accept-debt 无法区分该记谁的账。
        self.assertEqual(
            {"legacy_coordinate_exact", PROVENANCE_THIS_RUN},
            {i["provenance"] for i in payload["issues"]},
        )
        self.assertIn("provenance", csv_text.splitlines()[0])
        summary = payload["summary"]
        self.assertEqual(1, summary["new_error_count"])
        self.assertEqual(1, summary["carried_error_count"])



class DebtKeyIncludesDetailsTests(unittest.TestCase):
    """棘轮键必须带 details 维度（评估 R07）。

    实测：真机 851 条 `glossary_violation` 只压成 790 个 (sid, code) 键，53 个词条
    同时承载 2 条**不同术语**的违规，共 114 条。少了 details 维度，「同一词条同一
    code 上出现一条全新坏账」会免检放行 —— 债只能减不能增的语义被击穿。
    """

    def _record(self, term: str) -> QARecord:
        return QARecord(
            "glossary_violation",
            "error",
            f"reviewed glossary target is missing: {term}",
            "sid-1",
            "dogtags.mo",
            {"source_term": term, "required_target": "x"},
            "legacy_coordinate_exact",
        )

    def test_same_unit_same_code_different_term_is_a_separate_debt(self) -> None:
        first, second = self._record("Огненный волк"), self._record("Серебро")
        self.assertNotEqual(first.debt_key, second.debt_key)

    def test_details_key_order_does_not_change_the_key(self) -> None:
        # 规范化 JSON：字段顺序变了不能算成一笔新债，否则基线永远对不上。
        a = QARecord("c", "error", "m", "sid", "p", {"x": 1, "y": 2}, "unknown")
        b = QARecord("c", "error", "m", "sid", "p", {"y": 2, "x": 1}, "unknown")
        self.assertEqual(a.debt_key, b.debt_key)

    def test_a_brand_new_violation_on_a_baselined_unit_still_blocks(self) -> None:
        known, brand_new = self._record("Огненный волк"), self._record("Серебро")
        baseline = LegacyDebtBaseline([known.debt_key])
        gate = QualityGate(baseline)
        result = gate.evaluate([known, brand_new])
        self.assertFalse(result.passed)
        self.assertEqual(1, result.unaccepted_carried_count)

    def test_v1_baseline_is_refused_rather_than_silently_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "baseline.json"
            path.write_text(
                json.dumps({"schema_version": 1, "accepted": ["sid-1::code"]}),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx:
                LegacyDebtBaseline.load(path)
            self.assertIn("qa-accept-debt", str(ctx.exception))


class AcceptDebtCliPreconditionTests(unittest.TestCase):
    """`qa-accept-debt` 的前置条件必须自解释（评估 R25）。

    配置里没有 `quality_gate.legacy_debt_baseline` 时命令直接 BadParameter 退出，
    而 `require_release` 的提示只给了命令、没提这个前置条件 —— 第一个照提示操作
    的人必然撞墙。
    """

    def test_release_hint_names_the_missing_config_key(self) -> None:
        gate = QualityGate()  # 无基线路径
        carried = _error("sid-1", "glossary_violation", "legacy_coordinate_exact")
        with self.assertRaises(QualityGateError) as ctx:
            gate.require_release([carried])
        message = str(ctx.exception)
        self.assertIn("qa-accept-debt", message)
        self.assertIn("quality_gate.legacy_debt_baseline", message)

    def test_hint_omits_the_precondition_once_the_baseline_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            gate = QualityGate(LegacyDebtBaseline(path=Path(temp) / "b.json"))
            carried = _error("sid-1", "glossary_violation", "legacy_coordinate_exact")
            with self.assertRaises(QualityGateError) as ctx:
                gate.require_release([carried])
            self.assertNotIn("quality_gate.legacy_debt_baseline", str(ctx.exception))

    def test_example_project_keeps_the_strict_default_gate(self) -> None:
        # 通用示例不携带任何真实项目的存量债基线，默认保持 error 零容忍。
        import yaml

        path = Path(__file__).resolve().parents[1] / "projects" / "example" / "project.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertEqual({}, raw["quality_gate"])

class AcceptDebtRefusesNewKeysTests(unittest.TestCase):
    """`qa-accept-debt` 是**全量重写**基线（W0b）。

    不比对就写的话，任何新出现的坏账 —— 包括人工在审查面板里亲手写出来的 ——
    都会被整批登记成「已接受存量债」，棘轮「债只能减不能增」当场失效，
    事后只能翻 git 才知道发生过什么。
    """

    def _invoke(self, root, issues, *, allow_new_keys=False):
        import yaml
        from typer.testing import CliRunner

        from localizer.cli.main import app

        base = yaml.safe_load(
            (ROOT / "projects" / "example" / "project.yaml").read_text("utf-8")
        )
        (root / "game").mkdir(exist_ok=True)
        base["paths"] = {
            "source": str(root / "game"),
            "workspace": str(root / "ws"),
            "output": str(root / "out"),
        }
        base["quality_gate"] = {"legacy_debt_baseline": str(root / "baseline.json")}
        config = root / "project.yaml"
        config.write_text(
            yaml.safe_dump(base, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        report = root / "qa-report.json"
        report.write_text(
            json.dumps({"issues": issues, "summary": {}}, ensure_ascii=False),
            encoding="utf-8",
        )
        args = ["qa-accept-debt", str(config), str(report)]
        if allow_new_keys:
            args.append("--allow-new-keys")
        return CliRunner().invoke(app, args)

    @staticmethod
    def _issue(identity: str, term: str) -> dict:
        return {
            "code": "glossary_violation",
            "severity": "error",
            "message": "m",
            "stable_identity": identity,
            "relative_path": "ui.mo",
            "details": {"source_term": term, "required_target": "x"},
            "provenance": "legacy_coordinate_exact",
        }

    def test_new_keys_are_refused_without_the_flag(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = self._invoke(root, [self._issue("sid-1", "A")])
            self.assertEqual(0, first.exit_code, first.output)
            second = self._invoke(
                root, [self._issue("sid-1", "A"), self._issue("sid-2", "B")]
            )
            self.assertEqual(2, second.exit_code, second.output)
            self.assertIn("--allow-new-keys", second.output)
            # 基线未被改写。
            payload = json.loads((root / "baseline.json").read_text("utf-8"))
            self.assertEqual(1, payload["accepted_total"])

    def test_explicit_flag_lets_new_keys_through(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._invoke(root, [self._issue("sid-1", "A")])
            result = self._invoke(
                root,
                [self._issue("sid-1", "A"), self._issue("sid-2", "B")],
                allow_new_keys=True,
            )
            self.assertEqual(0, result.exit_code, result.output)
            payload = json.loads((root / "baseline.json").read_text("utf-8"))
            self.assertEqual(2, payload["accepted_total"])

    def test_shrinking_the_baseline_is_always_allowed(self) -> None:
        # 棘轮的方向：债只能减。清掉一条不该需要任何 flag。
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._invoke(
                root,
                [self._issue("sid-1", "A"), self._issue("sid-2", "B")],
                allow_new_keys=True,
            )
            result = self._invoke(root, [self._issue("sid-1", "A")])
            self.assertEqual(0, result.exit_code, result.output)
            payload = json.loads((root / "baseline.json").read_text("utf-8"))
            self.assertEqual(1, payload["accepted_total"])

    def test_report_without_provenance_warns(self) -> None:
        # provenance 落地之前的报告会让本该零容忍的机器新译被记成存量债。
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            issue = self._issue("sid-1", "A")
            del issue["provenance"]
            result = self._invoke(root, [issue])
            self.assertEqual(0, result.exit_code, result.output)
            self.assertIn("provenance", result.output)


if __name__ == "__main__":
    unittest.main()
