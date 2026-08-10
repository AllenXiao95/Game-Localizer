"""审查服务与它的 HTTP 面（W6 / W7）。

三条硬性不变量：
1. 落库前服务端**自己**重跑判据，不信客户端；新增 error 无 accepted_debt 一律拒绝。
2. `written < requested` 一律按错误返回，绝不显示「已落表」。
3. 永不输出 `QualityGateResult.passed`。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from localizer.adapters.storage.sqlite_tm import SQLiteTranslationMemory, TMEntry
from localizer.application.local_build import BuildMode, LocalBuildPipeline, ResourceBuild
from localizer.config import load_project_config
from localizer.domain.translation_unit import TranslationUnit
from localizer.web.collector import DashboardCollector
from localizer.web.review import ReviewConflict, ReviewService, ReviewUnavailable
from localizer.web.server import DashboardServer
from test_qa_judgement_extraction import _FakeAdapter

CASES = (
    ("menu.mo", "empty", "Пустой", ""),
    ("menu.mo", "gloss", "Серебро за бой", "战斗获得的钱"),
    ("menu.mo", "ph", "Всего %(count)s", "总共"),
    ("a.mo", "g1", "Общий текст", "译法甲"),
    ("b.mo", "g2", "Общий текст", "译法乙"),
    ("c.mo", "g3", "Общий текст", ""),
    ("m1.mo", "m1", "多数文本", "多数译法"),
    ("m2.mo", "m2", "多数文本", "多数译法"),
    ("m3.mo", "m3", "多数文本", "多数译法"),
    ("m4.mo", "m4", "多数文本", "少数译法"),
    ("m5.mo", "m5", "多数文本", "少数译法"),
)


def _unit(relative_path: str, key: str, source: str) -> TranslationUnit:
    return TranslationUnit(
        project_id="wot-ru-zh",
        adapter_id="gettext",
        relative_path=relative_path,
        logical_key=key,
        source_text=source,
        source_locale="ru-RU",
        target_locale="zh-Hans",
    )


class _Project:
    """一个能跑 build、能开 ReviewService 的最小项目。"""

    RUN_ID = "run-1"

    def __init__(self, root: Path, cases=CASES) -> None:
        self.root = root
        self.cases = tuple(cases)
        base = yaml.safe_load(
            (ROOT / "projects" / "example" / "project.yaml").read_text("utf-8")
        )
        (root / "game").mkdir(exist_ok=True)
        base["paths"] = {
            "source": str(root / "game"),
            "workspace": str(root / "ws"),
            "output": str(root / "out"),
        }
        glossary_file = root / "glossary.yaml"
        glossary_file.write_bytes(
            (ROOT / "tests" / "fixtures" / "scope-glossary.yaml").read_bytes()
        )
        base["glossary"]["file"] = str(glossary_file)
        base["rules"]["file"] = str(ROOT / "tests" / "fixtures" / "ru-rules.yaml")
        base["tm"]["database"] = str(root / "tm.sqlite3")
        base["review"] = {"decisions_file": str(root / "review" / "decisions.jsonl")}
        self.config_path = root / "project.yaml"
        self.config_path.write_text(
            yaml.safe_dump(base, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        self.config = load_project_config(self.config_path)

        self.units = tuple(_unit(rel, key, src) for rel, key, src, _ in self.cases)
        self.translations = {
            unit.stable_identity: text
            for unit, (_, _, _, text) in zip(self.units, self.cases)
        }
        from localizer.adapters.storage.glossary import GlossaryRepository
        from localizer.rules.loader import load_validation_rule

        pipeline = LocalBuildPipeline(
            validation_rule=load_validation_rule(
                self.config.rules.file, source_locale="ru-RU"
            ),
            glossary_terms=GlossaryRepository(self.config.glossary.file).load(),
        )
        by_file = {}
        for unit in self.units:
            by_file.setdefault(unit.relative_path, []).append(unit)
        resources = []
        for relative_path, group in sorted(by_file.items()):
            source = root / "game" / relative_path
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"")
            resources.append(ResourceBuild(_FakeAdapter(), source, tuple(group)))
        pipeline.build(
            resources,
            self.translations,
            mode=BuildMode.PREVIEW,
            project_id=self.config.project.id,
            run_id=self.RUN_ID,
            output_root=self.config.paths.output,
            unit_provenance={u.stable_identity: "machine" for u in self.units},
        )

    def service(self, *, is_busy=None) -> ReviewService:
        return ReviewService(
            self.config,
            output_root=self.config.paths.output,
            workspace_root=self.config.paths.workspace,
            is_busy=is_busy,
        )

    def identity(self, key: str) -> str:
        return next(u.stable_identity for u in self.units if u.logical_key == key)


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.project = _Project(Path(self._temp.name))
        self.service = self.project.service()

    def tearDown(self) -> None:
        self._temp.cleanup()

    def run_id(self) -> str:
        return _Project.RUN_ID


class SessionTests(_Case):
    def test_session_reports_the_measured_batch_leverage(self) -> None:
        session = self.service.session(self.run_id())
        self.assertTrue(session["available"])
        counters = session["counters"]
        # 批量杠杆的规模必须运行时算出来。把它说大了就是另一种假绿灯。
        self.assertEqual(
            counters["same_source_groups"],
            counters["groups_with_plurality"]
            + counters["groups_needing_case_by_case"],
        )
        self.assertGreaterEqual(counters["glossary_clusters"], 1)

    def test_session_says_same_source_does_not_unblock_release(self) -> None:
        # 不写清楚，操作者会先啃最大的那堆，干完发现照样发不出去。
        note = self.service.session(self.run_id())["notes"]["same_source"]
        self.assertIn("不解阻断", note)
        self.assertIn("warning", note)

    def test_missing_index_is_reported_not_raised(self) -> None:
        session = self.service.session("no-such-run")
        self.assertFalse(session["available"])
        self.assertIn("qa-review-index.json", session["reason"])

    def test_no_passed_field_anywhere(self) -> None:
        """面板永不输出 release 结论。"""
        payloads = [
            self.service.session(self.run_id()),
            self.service.groups(self.run_id()),
            self.service.glossary_clusters(self.run_id()),
            self.service.recheck(self.run_id(), {}),
        ]
        for payload in payloads:
            self.assertNotIn("passed", json.dumps(payload, ensure_ascii=False))


class QueryTests(_Case):
    def test_unit_detail_carries_server_computed_spans(self) -> None:
        payload = self.service.unit(self.run_id(), self.project.identity("gloss"))
        self.assertTrue(payload["glossary_spans"])
        span = payload["glossary_spans"][0]
        source = payload["source_text"]
        # 高亮由服务端算：前端另写一套正则必然与判据漂移。
        self.assertEqual(span["matched"], source[span["start"] : span["end"]])
        self.assertFalse(span["satisfied"])

    def test_unit_detail_carries_placeholder_spans(self) -> None:
        payload = self.service.unit(self.run_id(), self.project.identity("ph"))
        self.assertTrue(payload["placeholder_spans"])
        span = payload["placeholder_spans"][0]
        self.assertEqual(
            span["text"], payload["source_text"][span["start"] : span["end"]]
        )

    def test_group_rows_carry_variants_with_counts(self) -> None:
        groups = self.service.groups(self.run_id())["groups"]
        group = next(g for g in groups if g["source"] == "Общий текст")
        self.assertEqual(3, group["member_count"])
        self.assertTrue(group["has_empty_members"])
        self.assertEqual(
            {"译法甲", "译法乙", ""}, {v["translation"] for v in group["variants"]}
        )


    def test_units_view_filters_by_code(self) -> None:
        rows = self.service.units(self.run_id(), code="empty_translation")["units"]
        self.assertTrue(rows)
        for row in rows:
            self.assertIn("empty_translation", row["codes"])


class GlossaryExcludeTests(_Case):
    def _cluster(self):
        return next(
            cluster
            for cluster in self.service.glossary_clusters(self.run_id())["clusters"]
            if cluster["source_term"] == "Серебро"
        )

    def test_exclude_current_file_uses_guarded_maintenance_and_updates_live_projection(self) -> None:
        cluster = self._cluster()
        session = self.service.session(self.run_id())
        outcome = self.service.exclude_glossary_scope(
            self.run_id(),
            cluster["cluster_id"],
            "menu.mo",
            reason="这里是战斗结算语境，不采用该术语",
            expected_log_revision=session["log_revision"],
        )
        self.assertTrue(outcome["complete"])
        self.assertTrue(outcome["changed"])
        self.assertIn("menu.mo", outcome["exclude_scope"])

        refreshed = self._cluster()
        self.assertEqual(0, refreshed["violation_count"])
        self.assertIn("menu.mo", refreshed["exclude_scope"])
        unit = self.service.unit(self.run_id(), self.project.identity("gloss"))
        self.assertEqual([], unit["glossary_spans"])

        maintenance = self.project.config.glossary.file.parent / "glossary_maintenance"
        self.assertTrue(list(maintenance.glob("*.bak")))
        self.assertTrue(list(maintenance.glob("glossary-diff.*.json")))
        self.assertTrue((maintenance / "audit.jsonl").is_file())
        events = self.service.decisions(self.run_id())["decisions"]
        self.assertEqual("glossary", events[-1]["action"])
        self.assertEqual("add_exclude_scope", events[-1]["details"]["operation"])

    def test_review_panel_refuses_an_all_files_exclusion(self) -> None:
        cluster = self._cluster()
        with self.assertRaises(ValueError):
            self.service.exclude_glossary_scope(
                self.run_id(), cluster["cluster_id"], "*.mo", reason="太宽"
            )

class CommitGateTests(_Case):
    def _tm_rows(self, *identities):
        with SQLiteTranslationMemory(self.project.config.tm.database) as tm:
            return tm.rows_for(list(identities))

    def test_clean_edit_is_written_and_logged(self) -> None:
        identity = self.project.identity("empty")
        outcome = self.service.commit(
            self.run_id(), {identity: "补上的译文"}, reason="补译"
        )
        self.assertTrue(outcome.complete)
        self.assertEqual(1, outcome.written)
        rows = self._tm_rows(identity)
        self.assertEqual("补上的译文", rows[identity]["translation"])
        self.assertEqual("human", rows[identity]["origin"])
        decisions = self.service.decisions(self.run_id())["decisions"]
        self.assertEqual(1, len(decisions))
        self.assertEqual("补译", decisions[0]["reason"])
        # 前像必须留下，否则撤销无从谈起。
        self.assertEqual("committed", self.service.ledger(self.run_id()).state_of(identity))

    def test_introducing_an_error_is_refused_without_accepted_debt(self) -> None:
        identity = self.project.identity("empty")
        with self.assertRaises(ValueError) as ctx:
            self.service.commit(
                self.run_id(), {identity: "含\x00空字符"}, reason="随便写"
            )
        self.assertIn("accepted_debt", str(ctx.exception))
        self.assertEqual({}, self._tm_rows(identity), "被拒时一行都不该写")

    def test_accepted_debt_lets_it_through_and_records_the_reason(self) -> None:
        identity = self.project.identity("empty")
        outcome = self.service.commit(
            self.run_id(),
            {identity: "含\x00空字符"},
            reason="确认接受",
            accepted_debt={"codes": ["invalid_control_character"], "reason": "游戏内需要"},
        )
        self.assertEqual(1, outcome.written)
        event = self.service.decisions(self.run_id())["decisions"][0]
        self.assertEqual("accept_debt", event["action"])
        self.assertEqual("游戏内需要", event["details"]["accepted_debt"]["reason"])

    def test_server_does_not_trust_the_client(self) -> None:
        """客户端说没问题不算数 —— 服务端自己重跑一遍判据。"""
        import inspect

        source = inspect.getsource(ReviewService.commit)
        self.assertIn("self.rechecker(run_id).check(edits)", source)

    def test_unknown_identity_is_refused(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.service.commit(self.run_id(), {"nope": "译文"}, reason="x")
        self.assertIn("review index", str(ctx.exception))

    def test_empty_reason_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.service.commit(
                self.run_id(), {self.project.identity("empty"): "译文"}, reason="  "
            )

    def test_oversize_batch_is_refused(self) -> None:
        edits = {f"sid-{i}": "译文" for i in range(101)}
        with self.assertRaises(ValueError) as ctx:
            self.service.commit(self.run_id(), edits, reason="x")
        self.assertIn("100", str(ctx.exception))

    def test_running_task_blocks_writes(self) -> None:
        busy = self.project.service(is_busy=lambda: True)
        with self.assertRaises(ReviewConflict):
            busy.commit(self.run_id(), {self.project.identity("empty"): "译文"},
                        reason="x")

    def test_stale_log_revision_is_refused(self) -> None:
        from localizer.application.review_log import LogRevisionMismatch

        self.service.commit(
            self.run_id(), {self.project.identity("empty"): "第一次"}, reason="x"
        )
        with self.assertRaises(LogRevisionMismatch):
            self.service.commit(
                self.run_id(),
                {self.project.identity("gloss"): "战斗获得的银币"},
                reason="x",
                expected_log_revision="0:0000000000000000",
            )


class UnifyTests(_Case):
    def test_unify_writes_a_row_per_member_including_the_empty_one(self) -> None:
        """靠「写一条然后指望同源传播」在真机上不成立。

        `_resolve` 的第一分支是 `lookup()`，它对 legacy_clean 影子行照样返回，
        `lookup_reviewed_source` 根本轮不到。而且空译文成员不在 QA 记录里，
        漏掉就只统一了一半 —— 剩下的下一次运行被重译出第 N 种译法。
        """
        index = self.service.index(self.run_id())
        group = next(
            g for g in index.same_source_groups if g["source"] == "Общий текст"
        )
        outcome = self.service.unify(
            self.run_id(), group["group_id"], "统一译法", reason="定稿"
        )
        self.assertEqual(3, outcome.requested)
        self.assertEqual(3, outcome.written)
        with SQLiteTranslationMemory(self.project.config.tm.database) as tm:
            for member in group["members"]:
                self.assertEqual(
                    "统一译法", tm.lookup(member["stable_identity"]).translation
                )

    def test_unify_is_all_or_nothing_when_a_member_is_guarded(self) -> None:
        index = self.service.index(self.run_id())
        group = next(
            g for g in index.same_source_groups if g["source"] == "Общий текст"
        )
        blocked = group["members"][0]["stable_identity"]
        with SQLiteTranslationMemory(self.project.config.tm.database) as tm:
            tm.upsert(
                TMEntry(
                    stable_identity=blocked,
                    project_id=self.project.config.project.id,
                    adapter_id="gettext",
                    relative_path="a.mo",
                    logical_key="g1",
                    source_text="Общий текст",
                    source_fingerprint="fp",
                    translation="远端定稿",
                    origin="paratranz",
                    review_state="locked",
                    match_scope="coordinate_exact",
                    human_authored=True,
                    is_formal=True,
                )
            )
        outcome = self.service.unify(
            self.run_id(), group["group_id"], "统一译法", reason="定稿"
        )
        # 有成员被 guard 拦下时，整体必须按错误呈现，绝不显示「已落表」。
        self.assertFalse(outcome.complete)
        self.assertLess(outcome.written, outcome.requested)
        self.assertEqual(1, len(outcome.guarded))
        with SQLiteTranslationMemory(self.project.config.tm.database) as tm:
            self.assertEqual("远端定稿", tm.lookup(blocked).translation)

    def test_unknown_group_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.service.unify(self.run_id(), "nope", "译文", reason="x")

    def test_one_click_plurality_sync_updates_every_member_but_not_tied_groups(self) -> None:
        session = self.service.session(self.run_id())
        outcome = self.service.unify_majorities(
            self.run_id(),
            reason="采用非空译文中出现次数唯一最高的译法",
            expected_log_revision=session["log_revision"],
        )
        # 3:2 只有 60%，用于锁定审查批量操作不受历史 80% 收敛门槛限制。
        self.assertEqual("unique_non_empty_plurality", outcome["strategy"])
        self.assertIsNone(outcome["minimum_ratio"])
        self.assertEqual(1, outcome["groups_skipped_tied"])
        self.assertEqual(1, outcome["groups_eligible"])
        self.assertEqual(1, outcome["groups_completed"])
        self.assertEqual(5, outcome["items_requested"])
        self.assertEqual(5, outcome["items_written"])

        majority_ids = [self.project.identity(f"m{i}") for i in range(1, 6)]
        tied_ids = [self.project.identity(f"g{i}") for i in range(1, 4)]
        with SQLiteTranslationMemory(self.project.config.tm.database) as tm:
            rows = tm.rows_for(majority_ids + tied_ids)
        for identity in majority_ids:
            self.assertEqual("多数译法", rows[identity]["translation"])
            self.assertEqual("human", rows[identity]["origin"])
            self.assertEqual(1, rows[identity]["is_formal"])
        self.assertTrue(all(identity not in rows for identity in tied_ids))

        again = self.service.unify_majorities(
            self.run_id(),
            reason="再次同步应该跳过已解决组",
            expected_log_revision=outcome["log_revision"],
        )
        self.assertEqual(0, again["groups_eligible"])
        self.assertEqual(0, again["items_written"])

    def test_server_derived_bulk_is_not_limited_by_client_commit_size(self) -> None:
        cases = tuple(
            (
                f"large/{index:03d}.mo",
                f"large-{index:03d}",
                "超大同源组",
                "多数译法" if index < 51 else "少数译法",
            )
            for index in range(101)
        )
        large_root = Path(self._temp.name) / "large-project"
        large_root.mkdir()
        project = _Project(large_root, cases)
        service = project.service()
        outcome = service.unify_majorities(
            project.RUN_ID,
            reason="服务端索引派生的批量不受客户端 100 条正文上限影响",
        )
        self.assertEqual(1, outcome["groups_completed"])
        self.assertEqual(101, outcome["items_written"])
        with SQLiteTranslationMemory(project.config.tm.database) as tm:
            rows = tm.rows_for([unit.stable_identity for unit in project.units])
        self.assertEqual(101, len(rows))
        self.assertTrue(all(row["translation"] == "多数译法" for row in rows.values()))


class RevertTests(_Case):
    def test_revert_restores_the_before_image(self) -> None:
        identity = self.project.identity("empty")
        self.service.commit(self.run_id(), {identity: "写错了"}, reason="x")
        decision = self.service.decisions(self.run_id())["decisions"][0]
        result = self.service.revert(self.run_id(), [decision["decision_id"]])
        self.assertEqual(1, result["restored"])
        with SQLiteTranslationMemory(self.project.config.tm.database) as tm:
            self.assertEqual({}, tm.rows_for([identity]))
        self.assertEqual("reverted", self.service.ledger(self.run_id()).state_of(identity))

    def test_unknown_decision_id_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.service.revert(self.run_id(), ["nope"])


class MarkTests(_Case):
    def test_draft_skip_defer_do_not_touch_the_tm(self) -> None:
        identity = self.project.identity("empty")
        self.service.mark(
            self.run_id(),
            [
                {"target_id": identity, "action": "draft", "translation": "草稿"},
                {"target_id": "g-1", "action": "skip"},
                {"target_id": "g-2", "action": "defer"},
            ],
        )
        with SQLiteTranslationMemory(self.project.config.tm.database) as tm:
            self.assertEqual({}, tm.rows_for([identity]))
        ledger = self.service.ledger(self.run_id())
        self.assertEqual("draft", ledger.state_of(identity))
        self.assertEqual("skipped", ledger.state_of("g-1"))
        self.assertEqual("deferred", ledger.state_of("g-2"))
        # 草稿在服务端 —— 刷新页面、切 tab、误按 F5 都不会丢。
        self.assertEqual(["draft"], [ledger.items[identity]["state"]])

    def test_commit_action_is_not_accepted_by_mark(self) -> None:
        with self.assertRaises(ValueError):
            self.service.mark(
                self.run_id(), [{"target_id": "x", "action": "commit"}]
            )


class HttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.project = _Project(Path(self._temp.name))
        collector = DashboardCollector(
            self.project.config, self.project.config_path, ROOT
        )
        self.server = DashboardServer(collector, port=0).start_background()

    def tearDown(self) -> None:
        self.server.stop()
        self._temp.cleanup()

    def _url(self, path: str) -> str:
        return self.server.url.rstrip("/") + path

    def _get(self, path: str):
        with urllib.request.urlopen(self._url(path), timeout=10) as res:
            return res.status, json.loads(res.read().decode("utf-8"))

    def _post(self, path: str, payload: dict, *, header: bool = True, raw=None):
        body = raw if raw is not None else json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if header:
            headers["X-Localizer-Action"] = "1"
        request = urllib.request.Request(
            self._url(path), data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as res:
                return res.status, json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_review_get_routes(self) -> None:
        run = _Project.RUN_ID
        for path in (
            f"/api/runs/{run}/review/session",
            f"/api/runs/{run}/review/glossary",
            f"/api/runs/{run}/review/groups",
            f"/api/runs/{run}/review/units?code=empty_translation",
            f"/api/runs/{run}/review/decisions",
        ):
            with self.subTest(path=path):
                status, payload = self._get(path)
                self.assertEqual(200, status)
                self.assertTrue(payload.get("available"))

    def test_post_requires_the_action_header(self) -> None:
        status, payload = self._post(
            "/api/review/recheck", {"run_id": _Project.RUN_ID, "edits": {}},
            header=False,
        )
        self.assertEqual(403, status)
        self.assertEqual("action_header_required", payload["error"])

    def test_majority_sync_http_route(self) -> None:
        session = self._get(
            f"/api/runs/{_Project.RUN_ID}/review/session"
        )[1]
        status, payload = self._post(
            "/api/review/unify-majorities",
            {
                "run_id": _Project.RUN_ID,
                "reason": "批量采用高置信多数派",
                "expected_log_revision": session["log_revision"],
            },
        )
        self.assertEqual(200, status)
        self.assertEqual(1, payload["groups_completed"])
        self.assertEqual(5, payload["items_written"])
        self.assertEqual(1, payload["groups_skipped_tied"])

    def test_glossary_exclude_http_route(self) -> None:
        session = self._get(
            f"/api/runs/{_Project.RUN_ID}/review/session"
        )[1]
        clusters = self._get(
            f"/api/runs/{_Project.RUN_ID}/review/glossary"
        )[1]["clusters"]
        cluster = next(item for item in clusters if item["source_term"] == "Серебро")
        status, payload = self._post(
            "/api/review/glossary-exclude",
            {
                "run_id": _Project.RUN_ID,
                "cluster_id": cluster["cluster_id"],
                "path_glob": "menu.mo",
                "reason": "HTTP 中按当前语境排除",
                "expected_log_revision": session["log_revision"],
            },
        )
        self.assertEqual(200, status)
        self.assertTrue(payload["changed"])
        self.assertIn("menu.mo", payload["exclude_scope"])

    def test_oversize_body_is_413_not_400(self) -> None:
        """超限必须真的收到 413，而不是撞上 ConnectionReset。

        服务端不读 body 就回响应再关连接，客户端往往还在写 —— 它看到的是
        RST 而不是我们的 413。1 MiB 的体在开发机上大多能挤进 socket 缓冲区，
        所以这条测试曾经「大多数时候通过」，只在更慢的机器上间歇性失败。
        跑 5 遍是为了让这种时序问题不再靠运气蒙混过关。
        """
        for attempt in range(5):
            with self.subTest(attempt=attempt):
                status, payload = self._post(
                    "/api/review/commit", {}, raw=b"x" * ((1 << 20) + 10)
                )
                self.assertEqual(413, status)
                self.assertEqual("payload_too_large", payload["error"])

    def test_absurdly_large_body_is_not_fully_read(self) -> None:
        # 有界读走只覆盖「稍微超一点」的正常客户端；不能变成
        # 「攻击者让我们读多少就读多少」。
        from localizer.web.server import _DRAIN_MARGIN

        self.assertLessEqual(_DRAIN_MARGIN, 1 << 20)

    def test_commit_conflict_maps_to_409(self) -> None:
        status, payload = self._post(
            "/api/review/commit",
            {
                "run_id": _Project.RUN_ID,
                "edits": {self.project.identity("empty"): "译文"},
                "reason": "x",
                "expected_log_revision": "0:0000000000000000",
            },
        )
        # 空日志的 revision 就是这个值，所以第一次会成功；再来一次才冲突。
        status, payload = self._post(
            "/api/review/commit",
            {
                "run_id": _Project.RUN_ID,
                "edits": {self.project.identity("gloss"): "战斗获得的银币"},
                "reason": "x",
                "expected_log_revision": "0:0000000000000000",
            },
        )
        self.assertEqual(409, status)

    def test_recheck_response_is_never_authoritative(self) -> None:
        status, payload = self._post(
            "/api/review/recheck",
            {
                "run_id": _Project.RUN_ID,
                "edits": {self.project.identity("empty"): "补上的译文"},
            },
        )
        self.assertEqual(200, status)
        self.assertFalse(payload["authoritative"])
        self.assertEqual(
            ["quality_gate", "failed_unit_count", "legacy_debt_baseline"],
            payload["not_evaluated"],
        )

    def test_unknown_review_route_is_405(self) -> None:
        status, _ = self._post("/api/review/nuke", {"run_id": _Project.RUN_ID})
        self.assertEqual(405, status)

    def test_405_message_no_longer_claims_review_is_elsewhere(self) -> None:
        status, payload = self._post("/api/review/nuke", {})
        self.assertEqual(405, status)
        self.assertNotIn("人工审核仍在 ParaTranz 完成", payload["message"])
        self.assertIn("定点修复", payload["message"])


class NonLoopbackIsReadOnlyTests(unittest.TestCase):
    def test_review_writes_are_disabled_when_tasks_are(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            project = _Project(Path(temp))
            collector = DashboardCollector(project.config, project.config_path, ROOT)
            server = DashboardServer(collector, port=0, enable_tasks=False)
            try:
                self.assertIsNone(server.review)
                self.assertIsNone(server.tasks)
            finally:
                server.stop()


if __name__ == "__main__":
    unittest.main()
