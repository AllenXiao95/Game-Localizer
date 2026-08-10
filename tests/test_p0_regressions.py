"""P0 回归：来自 localizer-code-review 的 critical/high 缺陷。

每个用例都对应报告里的一条编号，并且**在修复前必须失败**——写的时候都先对未修复
的代码跑过一遍确认能抓住。选材刻意避开 happy path：报告的结论之一是 71 项测试全绿
却漏掉全部 high 以上缺陷，原因就是输入全挑了正常形态。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from localizer.adapters.storage.sqlite_tm import SQLiteTranslationMemory, TMEntry
from localizer.application.paratranz_sync import (
    ParaTranzItem,
    ParaTranzStagePolicy,
    ThreeWayParaTranzMerger,
    VALID_STAGES,
)
from localizer.config.models import ProjectConfig


def _raw(tm: SQLiteTranslationMemory, identity: str) -> dict:
    """直读一行。lookup() 带 is_formal/review_state 等命中过滤，
    而这里要断言的是「行本身有没有被机器覆盖」，两者不是一回事。"""
    row = tm.connection.execute(
        "SELECT * FROM tm_entries WHERE stable_identity = ?", (identity,)
    ).fetchone()
    return dict(row) if row is not None else {}


def _entry(**overrides) -> TMEntry:
    base = dict(
        stable_identity="unit-1",
        project_id="p",
        adapter_id="gettext",
        relative_path="ui.mo",
        logical_key="k",
        source_text="Полководец",
        source_fingerprint="fp1",
        translation="",
        origin="paratranz",
        review_state="reviewed",
        match_scope="coordinate_exact",
        classification="paratranz",
        stage=None,
        run_id=None,
        model=None,
        quality_state="passed",
        is_formal=False,
        human_authored=False,
    )
    base.update(overrides)
    return TMEntry(**base)


class C1RemoteEmptyMustNotClearLocal(unittest.TestCase):
    """C1 · paratranz_sync：远端空译文清空本地非空人工译文，且不留 conflict。

    这是 tools/paratranz.py 已修复事故在新内核里的回归；behavior-parity-matrix
    §4.1 把 P01–P06 标为「已完成」，但那 12 项回归测试守的是旧桥接。
    """

    def setUp(self) -> None:
        self.merger = ThreeWayParaTranzMerger()

    def test_every_stage_preserves_local_and_records_a_conflict(self) -> None:
        for stage in sorted(VALID_STAGES):
            with self.subTest(stage=stage):
                before = [ParaTranzItem("a", "Полководец", "统帅", 1, "human")]
                local = [ParaTranzItem("a", "Полководец", "统帅", 1, "human")]
                remote = [ParaTranzItem("a", "Полководец", "", stage, "human")]
                result = self.merger.merge(before, local, remote)
                self.assertEqual("统帅", result.merged[0].translation)
                self.assertEqual(
                    ["remote_empty_would_clear_local"],
                    [c.reason for c in result.conflicts],
                    msg="静默丢弃人工译文比丢弃本身更危险：必须留下冲突记录",
                )

    def test_whitespace_only_remote_counts_as_empty(self) -> None:
        result = self.merger.merge(
            [ParaTranzItem("a", "ru", "统帅", 1)],
            [ParaTranzItem("a", "ru", "统帅", 1)],
            [ParaTranzItem("a", "ru", "   \n ", 5)],
        )
        self.assertEqual("统帅", result.merged[0].translation)

    def test_explicit_human_resolution_can_still_accept_the_empty_remote(self) -> None:
        # 闸门只拦「没人裁决过的静默清空」，人工显式选 remote 必须仍然生效。
        result = self.merger.merge(
            [ParaTranzItem("a", "ru", "统帅", 1)],
            [ParaTranzItem("a", "ru", "统帅", 1)],
            [ParaTranzItem("a", "ru", "", 5)],
            resolutions={"a": "remote"},
        )
        self.assertEqual("", result.merged[0].translation)
        self.assertEqual((), result.conflicts)

    def test_non_empty_remote_is_still_adopted(self) -> None:
        result = self.merger.merge(
            [ParaTranzItem("a", "ru", "统帅", 1)],
            [ParaTranzItem("a", "ru", "统帅", 1)],
            [ParaTranzItem("a", "ru", "统帅（校对后）", 5)],
        )
        self.assertEqual("统帅（校对后）", result.merged[0].translation)
        self.assertEqual((), result.conflicts)

    def test_machine_candidate_upload_path_is_not_blocked_by_the_gate(self) -> None:
        # 修复的第一版把这条正常路径也拦了：本地机器候选 + 远端 stage 0 空
        # 是预翻译的主场景，必须照常上传为 stage 1。
        result = self.merger.merge(
            [ParaTranzItem("a", "ru", "", 0)],
            [ParaTranzItem("a", "ru", "机器候选", 0, origin="machine")],
            [ParaTranzItem("a", "ru", "", 0)],
        )
        self.assertEqual("机器候选", result.uploads[0].translation)
        self.assertEqual(1, result.uploads[0].stage)


class C2MachineMustNotOverwriteHumanEntries(unittest.TestCase):
    """C2 · sqlite_tm：upsert 的保护线画在 is_formal 上，而 stage 1/2/-1 的人工内容
    is_formal=0，会被机器译文连同 origin 一起覆盖。
    """

    def _roundtrip(self, stage: int) -> dict:
        formal = stage in (3, 5, 9)
        with tempfile.TemporaryDirectory() as temp:
            with SQLiteTranslationMemory(Path(temp) / "tm.sqlite3") as tm:
                tm.upsert(
                    _entry(
                        translation="统帅（人工）",
                        origin="paratranz",
                        stage=stage,
                        is_formal=formal,
                        human_authored=True,
                        review_state="reviewed" if formal else "suspect",
                    )
                )
                tm.upsert(
                    _entry(
                        translation="机器译文",
                        origin="machine",
                        review_state="unreviewed",
                        stage=None,
                        is_formal=False,
                        human_authored=False,
                        run_id="r1",
                        model="fake",
                    )
                )
                return _raw(tm, "unit-1")

    def test_human_entries_survive_a_machine_upsert_at_every_stage(self) -> None:
        for stage in (1, 2, -1, 3, 5, 9):
            with self.subTest(stage=stage):
                row = self._roundtrip(stage)
                self.assertEqual("统帅（人工）", row["translation"])
                self.assertEqual("paratranz", row["origin"])
                self.assertEqual(stage, row["stage"])

    def test_machine_may_still_replace_machine_and_untranslated_rows(self) -> None:
        cases = {
            "machine": dict(origin="machine", human_authored=False, stage=None),
            "stage_0": dict(origin="paratranz", human_authored=False, stage=0),
        }
        for label, seed in cases.items():
            with self.subTest(seed=label), tempfile.TemporaryDirectory() as temp:
                with SQLiteTranslationMemory(Path(temp) / "tm.sqlite3") as tm:
                    tm.upsert(_entry(translation="旧值", **seed))
                    tm.upsert(
                        _entry(
                            translation="新机器译文",
                            origin="machine",
                            human_authored=False,
                            stage=None,
                            run_id="r1",
                        )
                    )
                    self.assertEqual(
                        "新机器译文",
                        _raw(tm, "unit-1")["translation"],
                        msg="过度保护同样是缺陷：机器译文必须能刷新机器译文与未翻译占位行",
                    )

    def test_stage_policy_marks_every_touched_stage_as_human_authored(self) -> None:
        from localizer.domain.translation_unit import TranslationUnit

        policy = ParaTranzStagePolicy()
        for stage in sorted(VALID_STAGES):
            with self.subTest(stage=stage):
                unit = TranslationUnit(
                    project_id="p",
                    adapter_id="paratranz_json",
                    relative_path="ui.json",
                    logical_key="k",
                    source_text="source",
                    translation="译文",
                    source_locale="ru-RU",
                    target_locale="zh-Hans",
                    metadata={"stage": stage},
                )
                entry = policy.to_tm_entry(unit)
                # stage 0 是「未翻译」，机器可以填；其余都代表有人动过。
                self.assertEqual(stage != 0, entry.human_authored)


class SchemaMigrationTests(unittest.TestCase):
    """v1 库升级到 v2 时必须补上 human_authored 并回填，而不是报 unsupported。"""

    def test_v1_database_is_migrated_and_backfilled(self) -> None:
        import sqlite3

        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "tm.sqlite3"
            # 手工造一个 v1 库：无 human_authored 列，schema_version = 1。
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE tm_entries (
                    stable_identity TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    adapter_id TEXT NOT NULL, relative_path TEXT NOT NULL,
                    logical_key TEXT NOT NULL, source_text TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL, translation TEXT NOT NULL,
                    origin TEXT NOT NULL, review_state TEXT NOT NULL,
                    match_scope TEXT NOT NULL, classification TEXT NOT NULL,
                    stage INTEGER, run_id TEXT, model TEXT, prompt_hash TEXT,
                    rules_revision TEXT, glossary_revision TEXT,
                    quality_state TEXT NOT NULL, is_formal INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE legacy_sync (
                    source_path TEXT PRIMARY KEY, source_hash TEXT NOT NULL,
                    imported_count INTEGER NOT NULL, synced_at TEXT NOT NULL
                );
                INSERT INTO metadata VALUES ('schema_version', '1');
                INSERT INTO metadata VALUES ('authoritative', 'false');
                INSERT INTO tm_entries VALUES
                  ('human-1','p','a','ui.mo','k','ru','fp','统帅','paratranz','suspect',
                   'coordinate_exact','paratranz',2,NULL,NULL,NULL,NULL,NULL,'candidate',0,
                   't','t'),
                  ('machine-1','p','a','ui.mo','k2','ru','fp2','机器','machine','unreviewed',
                   'coordinate_exact','native',NULL,'r1',NULL,NULL,NULL,NULL,'passed',0,
                   't','t');
                """
            )
            connection.commit()
            connection.close()

            with SQLiteTranslationMemory(database) as tm:
                self.assertEqual(1, _raw(tm, "human-1")["human_authored"])
                self.assertEqual(0, _raw(tm, "machine-1")["human_authored"])
                # 迁移后立刻验证保护真的生效，而不是只看列存在。
                tm.upsert(
                    _entry(
                        stable_identity="human-1",
                        logical_key="k",
                        source_fingerprint="fp",
                        translation="机器覆盖",
                        origin="machine",
                        human_authored=False,
                        stage=None,
                        run_id="r2",
                    )
                )
                self.assertEqual("统帅", _raw(tm, "human-1")["translation"])


class H6CredentialEnvValidation(unittest.TestCase):
    """H6 · config：api_key_env 的校验实为「合法标识符检查」，主流真密钥原样通过。"""

    # 全部是**合成**的形状样本，只保留各家的前缀特征，主体是明显的占位内容。
    # 不要从真实（哪怕已泄露、已吊销）的凭据里截片段当测试数据：那会触发密钥
    # 扫描，也等于把真 token 的一部分再发布一次。
    REAL_KEY_SHAPES = (
        "AIzaNOTAREALKEYplaceholderAAAA",         # Google / Gemini
        "hf_NOTAREALKEYplaceholderAAAAAAAA",      # HuggingFace
        "sk_" + "live_NOTAREALKEYplaceholderAA", # Stripe / OpenAI 风格
        "ghp_NOTAREALKEYplaceholderAAAAAAA",      # GitHub PAT
        "github_pat_NOTAREALKEYplaceholder",      # GitHub 细粒度 PAT
        "xoxbNOTAREALKEYplaceholderAAAAAA",       # Slack
        "AK" + "IANOTAREALKEYPLACE",             # AWS access key id
    )

    def _config(self, api_key_env: str, root: Path) -> ProjectConfig:
        source = root / "source"
        source.mkdir(exist_ok=True)
        for name in ("prompt.md", "glossary.yaml", "rules.yaml"):
            (root / name).write_text("schema_version: 1\n", encoding="utf-8")
        data = {
            "schema_version": 1,
            "project": {"id": "g", "name": "G", "game_version": "1"},
            "paths": {
                "source": source,
                "workspace": root / "w",
                "output": root / "o",
            },
            "languages": {"source": "ru-RU", "target": "zh-Hans"},
            "resources": {"adapters": [{"type": "gettext"}]},
            "prompt": {"template": root / "prompt.md"},
            "glossary": {"file": root / "glossary.yaml"},
            "rules": {"file": root / "rules.yaml"},
            "provider": {
                "base_url": "https://p.invalid/v1",
                "api_key_env": api_key_env,
                "model": "m",
            },
            "tm": {"database": root / "tm.sqlite3"},
        }
        return (
            ProjectConfig.model_validate(data)
            if hasattr(ProjectConfig, "model_validate")
            else ProjectConfig.parse_obj(data)
        )

    def test_real_looking_secrets_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for secret in self.REAL_KEY_SHAPES:
                with self.subTest(secret=secret[:12]):
                    with self.assertRaises(Exception):
                        self._config(secret, root)

    def test_conventional_env_var_names_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("SLICONFLOW_API_KEY", "OPENAI_API_KEY", "LOCALIZER_KEY_1"):
                with self.subTest(name=name):
                    self.assertEqual(
                        name, self._config(name, root).provider.api_key_env
                    )


class H7CredentialsNeverInMessages(unittest.TestCase):
    """H7 · github_release：未设置的 token 变量名被原样拼进异常消息。

    变量名本身是无害的，但 H6 之前允许把真 PAT 填进 token_env——两个缺陷叠加
    就是把真凭据打进 CI 日志。即使 H6 收紧后，异常消息里也不该出现取值。
    """

    def test_missing_token_error_does_not_echo_the_variable_value(self) -> None:
        from localizer.adapters.publishers.github_release import (
            UrllibGitHubReleaseClient,
        )

        sentinel = "GH_TOKEN_SENTINEL_FOR_TEST"
        os.environ.pop(sentinel, None)
        with self.assertRaises(ValueError) as ctx:
            # 构造 client 不发网络请求，正好只打凭据这条路径。
            UrllibGitHubReleaseClient(token_env=sentinel)
        self.assertNotIn(sentinel, str(ctx.exception))


class H2RulesReachTheTranslationStage(unittest.TestCase):
    """H2 · BatchOrchestrator 硬编码空 ValidationRule，一次运行存在两套规则。

    后果不是坏数据流出（那是 fail-open），而是命中白名单的**正确**译文在翻译阶段
    被判 source_language_residue → failed → release 被永久阻断，且错误文案写着
    "not allowed by rules.yaml"，而 rules.yaml 明确允许它。
    """

    def test_orchestrator_accepts_an_injected_rule(self) -> None:
        import inspect

        from localizer.application.batch_orchestrator import BatchOrchestrator

        self.assertIn(
            "validation_rule",
            inspect.signature(BatchOrchestrator.__init__).parameters,
            msg="没有注入口，调用方就只能用硬编码的空规则",
        )

    def test_allowlisted_term_survives_the_translation_stage(self) -> None:
        from localizer.application.batch_orchestrator import (
            BatchOrchestrator,
            JsonCheckpoint,
        )
        from localizer.application.prompt import PromptComposer
        from localizer.domain.translation_unit import TranslationUnit
        from localizer.ports.provider import ProviderResponse
        from localizer.rules.validation import ValidationRule

        class Provider:
            def __init__(self) -> None:
                self.calls = 0

            def translate(self, prompt, units):
                self.calls += 1
                # 模型给出正确译文：型号 КВ-1 按规则原样保留。
                return ProviderResponse("\n".join(["[1] 重型坦克 КВ-1", "---END---"]))

        unit = TranslationUnit(
            project_id="p",
            adapter_id="gettext",
            relative_path="ui.mo",
            logical_key="k",
            source_text="Тяжёлый танк КВ-1",
            translation="",
            source_locale="ru-RU",
            target_locale="zh-Hans",
        )
        with tempfile.TemporaryDirectory() as temp:
            provider = Provider()
            result = BatchOrchestrator(
                provider,
                PromptComposer("translate", "", ""),
                JsonCheckpoint(Path(temp) / "checkpoint.json"),
                validation_rule=ValidationRule(cyrillic_exact_allowlist=("КВ-1",)),
            ).run([unit])
            self.assertEqual("succeeded", result.results[0].state)
            self.assertEqual("重型坦克 КВ-1", result.results[0].translation)
            self.assertEqual(
                1, provider.calls, msg="误判会触发多余的 QA 重试，白烧一次调用"
            )


class H3AllowlistIsWholeTermNotSubstring(unittest.TestCase):
    """H3 · exact_allowlist 实为全文子串剥离，且 loader 零类型校验。"""

    def _residue(self, rule, text: str) -> bool:
        summary = rule.validate_text(
            text, adapter_id="gettext", relative_path="ui.mo"
        )
        return any(i.code == "source_language_residue" for i in summary.issues)

    def test_allowlist_matches_whole_terms_only(self) -> None:
        from localizer.rules.validation import ValidationRule

        rule = ValidationRule(cyrillic_exact_allowlist=("КВ-1",))
        for text, should_pass in (
            ("重型坦克 КВ-1", True),        # 白名单整词命中
            ("重型坦克 (КВ-1)", True),      # 带标点仍算整词
            ("重型坦克 КВ-1234", False),    # 前缀相同但不是同一个词
            ("重型坦克 xКВ-1x", False),     # 白名单被当成子串
            ("Тяжёлый танк", False),       # 完全未翻译
        ):
            with self.subTest(text=text):
                self.assertEqual(not should_pass, self._residue(rule, text))

    def test_loader_rejects_degenerate_and_unknown_shapes(self) -> None:
        from localizer.rules.loader import RulesLoadError, load_validation_rule

        bad = {
            # 少写一个 "- " —— YAML 合法，语义完全变了：逐字符白名单。
            "scalar_allowlist": "schema_version: 1\ncyrillic:\n  exact_allowlist: Танк\n",
            # 原实现对这两种抛的是裸 TypeError，错误信息完全指不到问题所在。
            "int_allowlist": "schema_version: 1\ncyrillic:\n  exact_allowlist: 5\n",
            "list_mappings": "schema_version: 1\ncyrillic:\n  mappings: [a, b]\n",
            # 未知顶层键此前被静默丢弃，配置作者会以为规则生效了。
            "unknown_section": "schema_version: 1\nplaceholder_rules: []\n",
        }
        with tempfile.TemporaryDirectory() as temp:
            for label, body in bad.items():
                with self.subTest(shape=label):
                    path = Path(temp) / f"{label}.yaml"
                    path.write_text(body, encoding="utf-8")
                    with self.assertRaises(RulesLoadError):
                        load_validation_rule(path)

    def test_loader_accepts_the_repository_rules_file(self) -> None:
        from localizer.rules.loader import load_validation_rule

        rule = load_validation_rule(ROOT / "tests" / "fixtures" / "ru-rules.yaml")
        self.assertEqual([], list(rule.cyrillic_exact_allowlist))


class H4RunIdIsPathSafeAndReleasesAreImmutable(unittest.TestCase):
    """H4 · run_id 未校验直接拼路径；同 run_id 重跑静默覆盖已发布制品。

    路径穿越那一半的威胁模型是运维/CI 传入的 --run-id，不是外部不可信输入；
    但「重跑覆盖」那一半不需要任何恶意输入，是纯发布完整性缺陷。
    """

    def test_rejects_traversal_and_separators(self) -> None:
        from localizer.infrastructure.workspace import validate_run_id

        for bad in (
            "../../../pwned",   # 实测能把 zip 与 manifest 写到 paths.output 之外
            "feature/x",        # CI 常见；旧实现在 mkstemp 处炸出难懂的 FileNotFoundError
            "a\\b",
            "",
            "-leading",
            "x" * 65,
        ):
            with self.subTest(run_id=bad), self.assertRaises(ValueError):
                validate_run_id(bad)

    def test_accepts_conventional_run_ids(self) -> None:
        from localizer.infrastructure.workspace import validate_run_id

        for good in ("release-001", "preview_2026.01", "a", "RC1"):
            with self.subTest(run_id=good):
                self.assertEqual(good, validate_run_id(good))

    def test_release_refuses_to_overwrite_an_existing_run(self) -> None:
        from localizer.application.local_build import BuildMode, LocalBuildPipeline

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "out"
            (output / "release" / "release-001").mkdir(parents=True)
            with self.assertRaises(FileExistsError):
                LocalBuildPipeline().build(
                    [],
                    {},
                    mode=BuildMode.RELEASE,
                    project_id="p",
                    run_id="release-001",
                    output_root=output,
                )

    def test_preview_may_be_rerun_freely(self) -> None:
        from localizer.application.local_build import BuildMode, LocalBuildPipeline

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "out"
            for _ in range(2):
                result = LocalBuildPipeline().build(
                    [],
                    {},
                    mode=BuildMode.PREVIEW,
                    project_id="p",
                    run_id="preview-001",
                    output_root=output,
                )
            self.assertIsNone(result.bundle)


class H13MultiTargetPublishIsolatesFailures(unittest.TestCase):
    """H13 · publish.targets 是死配置：publisher_from_config 生产路径零引用。

    配了 local + github + r2 + oss 多个目标，实际只有 publish-local 执行，退出码 0、无告警。
    F24 要求的「一个目标失败不影响其余、不触碰本地制品、可单独重试」在旧 API 下
    连表达都做不到——PublishReceipt 只有 target 和 objects。
    """

    def _bundle(self, temp: Path):
        from localizer.application.artifact import ArtifactBuilder

        resources = temp / "res"
        resources.mkdir(parents=True)
        payload = resources / "ui.mo"
        payload.write_bytes(b"payload")
        return ArtifactBuilder().build_release(
            project_id="p",
            run_id="r1",
            resource_root=resources,
            resource_paths=[payload],
            destination=temp / "out",
        )

    def _section(self, *types: str):
        from localizer.config.models import PublishSection

        examples = {
            "local": {"type": "local", "destination": "d"},
            "github_release": {
                "type": "github_release",
                "repository": "owner/repo",
                "tag": "v1",
                "token_env": "GITHUB_TOKEN",
            },
            "cloudflare_r2": {
                "type": "cloudflare_r2",
                "account_id": "account",
                "bucket": "bucket",
                "access_key_env": "R2_ACCESS_KEY_ID",
                "secret_key_env": "R2_SECRET_ACCESS_KEY",
            },
            "alibaba_oss": {
                "type": "alibaba_oss",
                "endpoint": "https://oss.invalid",
                "bucket": "bucket",
                "sts_token_url": "https://sts.invalid/token",
                "sts_token_env": "OSS_API_TOKEN_HEADER",
            },
        }
        data = {"targets": [examples[target_type] for target_type in types]}
        return (
            PublishSection.model_validate(data)
            if hasattr(PublishSection, "model_validate")
            else PublishSection.parse_obj(data)
        )

    def test_one_failing_target_does_not_stop_the_others_or_touch_the_artifact(self):
        from localizer.application.publish import PublishOrchestrator
        from localizer.ports.publisher import PublishReceipt

        with tempfile.TemporaryDirectory() as temp:
            bundle = self._bundle(Path(temp))
            before = bundle.artifact.read_bytes()

            class Flaky:
                def __init__(self, kind: str) -> None:
                    self.kind = kind

                def publish(self, _bundle) -> PublishReceipt:
                    if self.kind == "github_release":
                        raise ConnectionError("502 from GitHub")
                    if self.kind == "cloudflare_r2":
                        raise NameError("bucket_nmae")  # 拼写错误，不是网络问题
                    return PublishReceipt(self.kind, ())

            results = PublishOrchestrator(factory=lambda t: Flaky(t.type)).publish(
                bundle,
                self._section(
                    "local", "github_release", "cloudflare_r2", "alibaba_oss"
                ),
            )

            by_target = {r.target: r for r in results}
            self.assertEqual(4, len(results), "失败的目标不得中断后续目标")
            self.assertTrue(by_target["local"].succeeded)
            self.assertTrue(by_target["alibaba_oss"].succeeded)

            self.assertFalse(by_target["github_release"].succeeded)
            self.assertTrue(
                by_target["github_release"].retryable, "网络类失败应可重试"
            )

            self.assertFalse(by_target["cloudflare_r2"].succeeded)
            self.assertEqual(
                "internal",
                by_target["cloudflare_r2"].error_class,
                msg="NameError 不得被伪装成 upload_failed",
            )
            self.assertFalse(
                by_target["cloudflare_r2"].retryable, "程序错误重试一万次还是这个错"
            )

            self.assertEqual(
                before, bundle.artifact.read_bytes(), "发布失败不得触碰本地制品"
            )

    def test_preview_bundles_are_refused_before_any_target_runs(self) -> None:
        import dataclasses

        from localizer.application.publish import PublishOrchestrator

        with tempfile.TemporaryDirectory() as temp:
            bundle = dataclasses.replace(self._bundle(Path(temp)), mode="preview")
            calls = []

            def factory(target):
                calls.append(target.type)
                raise AssertionError("unreachable")

            with self.assertRaises(ValueError):
                PublishOrchestrator(factory=factory).publish(
                    bundle, self._section("local")
                )
            self.assertEqual([], calls, "闸门未过的产物不该到达任何 Publisher")


class H11H12BatchRetryDoesNotAmplifyLoad(unittest.TestCase):
    """H11/H12 · 网络类失败耗尽后错误地二分缩批；永久错误不计预算、不缩批。"""

    def _units(self, count: int):
        from localizer.domain.translation_unit import TranslationUnit

        return [
            TranslationUnit(
                project_id="p",
                adapter_id="gettext",
                relative_path="ui.mo",
                logical_key=f"k{i}",
                source_text=f"Текст {i}",
                translation="",
                source_locale="ru-RU",
                target_locale="zh-Hans",
            )
            for i in range(count)
        ]

    def _run(self, provider, units, **kwargs):
        from localizer.application.batch_orchestrator import (
            BatchOrchestrator,
            JsonCheckpoint,
        )
        from localizer.application.prompt import PromptComposer

        with tempfile.TemporaryDirectory() as temp:
            return BatchOrchestrator(
                provider,
                PromptComposer("translate", "", ""),
                JsonCheckpoint(Path(temp) / "checkpoint.json"),
                sleep=lambda _seconds: None,
                **kwargs,
            ).run(units)

    def test_persistent_throttling_stops_after_the_retry_budget(self) -> None:
        from localizer.adapters.providers.openai_compatible import (
            TransientProviderError,
        )

        class Throttled:
            def __init__(self) -> None:
                self.calls = 0

            def translate(self, prompt, units):
                self.calls += 1
                raise TransientProviderError("429 Too Many Requests")

        provider = Throttled()
        result = self._run(provider, self._units(16), max_transient_retries=2)
        # 原实现在这里打 93 次：耗尽后二分重投，每层递归又把 transient_attempt
        # 和退避一起归零。对正在限流的端点连打 93 次，最终一条译文都拿不到。
        self.assertEqual(3, provider.calls, "= max_transient_retries + 1，不得缩批放大")
        self.assertTrue(all(r.state != "succeeded" for r in result.results))

    def test_oversized_request_is_split_until_it_fits(self) -> None:
        from localizer.adapters.providers.openai_compatible import (
            PermanentProviderError,
        )
        from localizer.ports.provider import ProviderResponse

        class Oversized:
            def __init__(self) -> None:
                self.sizes = []

            def translate(self, prompt, units):
                self.sizes.append(len(units))
                if len(units) > 4:
                    raise PermanentProviderError("HTTP 400: context_length_exceeded")
                lines = [f"[{i + 1}] 译文{i + 1}" for i in range(len(units))]
                return ProviderResponse("\n".join([*lines, "---END---"]))

        provider = Oversized()
        result = self._run(provider, self._units(16))
        self.assertTrue(
            all(r.state == "succeeded" for r in result.results),
            msg="尺寸类错误缩批就能救回来，原实现一次性判死整批",
        )
        self.assertGreater(result.requests, 0, "请求确实发出去了，必须计入预算")

    def test_non_size_permanent_error_is_not_split(self) -> None:
        from localizer.adapters.providers.openai_compatible import (
            PermanentProviderError,
        )

        class Unauthorized:
            def __init__(self) -> None:
                self.calls = 0

            def translate(self, prompt, units):
                self.calls += 1
                raise PermanentProviderError("HTTP 401: invalid api key")

        provider = Unauthorized()
        result = self._run(provider, self._units(16))
        self.assertEqual(1, provider.calls, "认证失败缩批只是白烧钱")
        self.assertEqual(1, result.requests)


class H5SaveTmCannotBeAbbreviatedPastTheFacade(unittest.TestCase):
    """H5 · 门面用字面量匹配拦 --save-tm，而 argparse 默认 allow_abbrev=True。

    绕过面比初看更宽：任何以 --s 开头的前缀都唯一匹配 --save-tm。
    """

    def _policy(self, phase_name: str):
        from localizer.compat.legacy import LegacyAccessPolicy, LegacyPhase

        return LegacyAccessPolicy(LegacyPhase(phase_name))

    def test_every_unique_abbreviation_is_blocked(self) -> None:
        for phase in ("m3_shadow", "sqlite_authority"):
            policy = self._policy(phase)
            for argv in (
                ["--save-tm"],
                ["--save"],
                ["--save-t"],
                ["--sa"],
                ["--s"],
                ["--save-tm=1"],          # 等号形式，原实现同样漏判
                ["--env", "RU", "--sav"],
            ):
                with self.subTest(phase=phase, argv=argv):
                    with self.assertRaises(RuntimeError):
                        policy.validate_arguments(argv)

    def test_unrelated_options_still_pass(self) -> None:
        policy = self._policy("sqlite_authority")
        for argv in (
            ["--version", "1.0"],
            ["--env", "RU"],
            ["--force-release"],
            ["--no-auto-glossary"],
            [],
        ):
            with self.subTest(argv=argv):
                policy.validate_arguments(argv)  # 不应抛异常

    def test_legacy_entrypoint_disables_prefix_abbreviation(self) -> None:
        # 门面之外再加一道：旧入口自己也不该接受缩写，否则新增选项时
        # 消歧规则一变，门面的镜像实现就会和实际行为分叉。
        legacy = ROOT / "multi_i18n_processor_v6.py"
        if not legacy.is_file():
            # 独立新框架项目结构性退役旧入口后，这条风险已不可达。
            self.assertFalse(legacy.exists())
            return
        source = legacy.read_text(encoding="utf-8")
        self.assertIn("allow_abbrev=False", source)


class H15PlaceholderVariantResidueIsCaught(unittest.TestCase):
    """H15 · restore 之后没有任何残留扫描，全角占位符变体直达 .mo。

    find_tokens 的多重集比较只认严格半角小写形式。模型把占位符「中文化」一份、
    同时保留半角一份时，半角计数仍然 1:1 相等，restore 也不认识全角那份 ——
    玩家在游戏 UI 上会直接看到字面量「【PH_d9e14c98_0】」。
    """

    def test_variant_forms_are_detected(self) -> None:
        from localizer.rules.placeholder import PlaceholderRule

        rule = PlaceholderRule()
        for text in (
            "剩余【PH_d9e14c98_0】%(count)d天",   # U10 记录的真实模型行为
            "剩余 [ph_d9e14c98_0] 天",
            "剩余 [PH d9e14c98 0] 天",
            "剩余（PH_d9e14c98_0）天",
        ):
            with self.subTest(text=text):
                self.assertTrue(rule.find_token_residue(text))

    def test_ordinary_bracketed_text_is_not_flagged(self) -> None:
        from localizer.rules.placeholder import PlaceholderRule

        rule = PlaceholderRule()
        for text in (
            "剩余 %(count)d 天",
            "获得 [VARIANT] 奖励",       # 游戏文本里合法的方括号写法
            "触发 [BUFF_0] 效果",
            "看 [PH_zzzz_0] 这个",       # 非十六进制，不是我们的 token
        ):
            with self.subTest(text=text):
                self.assertEqual((), rule.find_token_residue(text))

    def test_orchestrator_fails_the_unit_instead_of_shipping_the_literal(self) -> None:
        import re

        from localizer.application.batch_orchestrator import (
            BatchOrchestrator,
            JsonCheckpoint,
        )
        from localizer.application.prompt import PromptComposer
        from localizer.domain.translation_unit import TranslationUnit
        from localizer.ports.provider import ProviderResponse

        class Sloppy:
            def translate(self, prompt, units):
                token = re.search(r"\[PH_[0-9a-f]{8}_\d+\]", units[0].source_text)
                half = token.group(0) if token else ""
                full = half.replace("[", "【").replace("]", "】")
                # 半角保留一份 -> 多重集比较通过；全角那份 restore 不认识。
                return ProviderResponse(f"[1] 剩余{full}{half}天\n---END---")

        unit = TranslationUnit(
            project_id="p",
            adapter_id="gettext",
            relative_path="ui.mo",
            logical_key="k",
            source_text="Осталось %(count)d дней",
            translation="",
            source_locale="ru-RU",
            target_locale="zh-Hans",
        )
        with tempfile.TemporaryDirectory() as temp:
            result = BatchOrchestrator(
                Sloppy(),
                PromptComposer("translate", "", ""),
                JsonCheckpoint(Path(temp) / "checkpoint.json"),
                sleep=lambda _s: None,
            ).run([unit])
        self.assertEqual("failed", result.results[0].state)
        self.assertIn(
            "placeholder_variant_residue",
            [issue.code for issue in result.results[0].issues],
        )

    def test_build_stage_checks_too_because_tm_hits_skip_the_orchestrator(self) -> None:
        """TM 命中与 ParaTranz 回流的译文不经过编排器，构建阶段是它们唯一的关口。

        改成**行为断言**而不是源码文本断言：W1 把判据搬进了 `inspect_unit`，
        原来那条 `"find_token_residue" in build 的源码` 会因为搬家而失配，
        但它本来想守的是「构建期确实会查残留」这件事 —— 直接跑一遍更可靠。
        """
        from localizer.application.local_build import LocalBuildPipeline
        from localizer.domain.translation_unit import TranslationUnit

        unit = TranslationUnit(
            project_id="p",
            adapter_id="gettext",
            relative_path="ui.mo",
            logical_key="k",
            source_text="Привет",
            source_locale="ru-RU",
            target_locale="zh-Hans",
        )
        # 这条译文是「TM 命中」的形态：没经过编排器，token 以变体形式残留。
        inspection = LocalBuildPipeline().inspect_unit(
            unit, "你好【PH_d9e14c98_0】", "legacy_coordinate_exact"
        )
        self.assertIn(
            "placeholder_variant_residue",
            [record.code for record in inspection.records],
        )
        # 并且 build() 确实走这条判据（否则上面的断言只证明了函数存在）。
        import inspect as _inspect

        self.assertIn(
            "inspect_unit", _inspect.getsource(LocalBuildPipeline.build)
        )


if __name__ == "__main__":
    unittest.main()
