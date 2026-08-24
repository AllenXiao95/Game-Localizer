"""远端发布的凭据事件治理必须由代码强制执行。

测试覆盖默认不预设泄露、显式事件的 fail-closed 语义，以及单目标隔离。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from localizer.application.artifact import ReleaseBundle
from localizer.application.publish import PublishOrchestrator, publisher_from_config
from localizer.config.models import (
    GovernanceError,
    PublishSection,
    PublishTargetSection,
    SecuritySection,
)

REMOTE_TYPES = ("github_release", "cloudflare_r2", "alibaba_oss")


def _remote_target(target_type: str) -> PublishTargetSection:
    if target_type == "github_release":
        return PublishTargetSection(
            type="github_release",
            repository="org/repo",
            tag="v1",
            token_env="GITHUB_TOKEN",
        )
    if target_type == "cloudflare_r2":
        return PublishTargetSection(
            type="cloudflare_r2",
            bucket="b",
            account_id="acc",
            access_key_env="R2_ACCESS_KEY",
            secret_key_env="R2_SECRET_KEY",
        )
    return PublishTargetSection(
        type="alibaba_oss",
        endpoint="https://oss-cn.example.com",
        bucket="b",
        sts_token_url="https://sts.example.com",
        sts_token_env="OSS_STS_TOKEN",
    )


class SecuritySectionTests(unittest.TestCase):
    def test_default_does_not_assume_a_credential_incident(self) -> None:
        section = SecuritySection()
        self.assertTrue(section.remote_publishing_allowed)
        section.assert_remote_publishing_allowed("github_release")

    def test_declared_rotation_event_stays_closed_without_a_record(self) -> None:
        # 只填日期不留记录，事后无法核对到底轮换了哪几套 —— 那不算治理完成。
        section = SecuritySection(
            credential_rotation_required=True,
            credential_rotation_completed_at="2026-08-01",
        )
        self.assertFalse(section.remote_publishing_allowed)
        with self.assertRaises(GovernanceError) as ctx:
            section.assert_remote_publishing_allowed("github_release")
        self.assertIn("rotation_record", str(ctx.exception))

    def test_both_fields_open_the_gate(self) -> None:
        section = SecuritySection(
            credential_rotation_required=True,
            credential_rotation_completed_at="2026-08-01",
            rotation_record="docs/security/rotation-2026-08.md",
        )
        self.assertTrue(section.remote_publishing_allowed)
        section.assert_remote_publishing_allowed("github_release")  # 不抛

    def test_malformed_date_is_rejected_at_config_load(self) -> None:
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            SecuritySection(credential_rotation_completed_at="上周")


class FactoryGateTests(unittest.TestCase):
    def test_remote_targets_are_refused_before_credentials_are_read(self) -> None:
        # 必须在**构造之前**拦下：构造 GitHubReleasePublisher 就已经把 token
        # 从环境读出来了。
        for target_type in REMOTE_TYPES:
            with self.subTest(target=target_type):
                with self.assertRaises(GovernanceError):
                    publisher_from_config(
                        _remote_target(target_type),
                        security=SecuritySection(credential_rotation_required=True),
                    )

    def test_local_target_is_never_gated(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            publisher_from_config(
                PublishTargetSection(type="local", destination=Path(temp)),
                security=SecuritySection(credential_rotation_required=True),
            )


class OrchestratorIsolationTests(unittest.TestCase):
    """治理拒绝要保持 F24 的单目标隔离，且必须 retryable=False。"""

    def _bundle(self, root: Path) -> ReleaseBundle:
        artifact = root / "a.zip"
        with zipfile.ZipFile(artifact, "w") as archive:
            archive.writestr("x.mo", "x")
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        manifest = root / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "project_id": "p",
                    "run_id": "r",
                    "mode": "release",
                    "quality_gate_passed": True,
                    "artifact": {"name": artifact.name, "sha256": digest},
                    "resources": [],
                }
            ),
            encoding="utf-8",
        )
        return ReleaseBundle.load(manifest)

    def test_local_still_publishes_while_remote_is_governance_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = self._bundle(root)
            section = PublishSection(
                targets=[
                    PublishTargetSection(type="local", destination=root / "out"),
                    *[_remote_target(t) for t in REMOTE_TYPES],
                ]
            )
            results = PublishOrchestrator(
                security=SecuritySection(credential_rotation_required=True)
            ).publish(bundle, section)
        by_target = {r.target: r for r in results}
        self.assertEqual("succeeded", by_target["local"].status)
        for target_type in REMOTE_TYPES:
            result = by_target[target_type]
            self.assertEqual("failed", result.status)
            self.assertEqual("governance", result.error_class)
            # 治理拒绝不是网络故障：标成 retryable 会让运维反复重试一个
            # 重试一万次也不会变的东西。
            self.assertFalse(result.retryable)

    def test_default_reaches_provider_credential_validation(self) -> None:
        # 没有声明凭据事件时不应伪装成治理拦截；Provider 仍会独立检查凭据。
        with patch.dict(os.environ, {}, clear=True):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                results = PublishOrchestrator().publish(
                    self._bundle(root),
                    PublishSection(targets=[_remote_target("github_release")]),
                )
        self.assertEqual("ValueError", results[0].error_class)
        self.assertIn("environment variable is unset", results[0].error_message)


class ShippedConfigDeclaresGovernanceTests(unittest.TestCase):
    def test_example_config_does_not_declare_a_credential_incident(self) -> None:
        """通用示例只启用本地发布，也不会凭空声明凭据已经泄露。"""
        import yaml

        path = ROOT / "projects" / "example" / "project.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertFalse(raw["security"]["credential_rotation_required"])
        self.assertIsNone(raw["security"]["credential_rotation_completed_at"])
        self.assertEqual(["local"], [target["type"] for target in raw["publish"]["targets"]])

    def test_pre_commit_config_runs_a_secret_scanner(self) -> None:
        text = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
        self.assertIn("gitleaks", text)

    def test_ci_scans_the_full_history(self) -> None:
        text = (ROOT / ".github" / "workflows" / "tests.yml").read_text("utf-8")
        self.assertIn("gitleaks", text)
        # 只扫增量的话，已经在历史里的凭据永远不会被发现。
        self.assertIn("fetch-depth: 0", text)


if __name__ == "__main__":
    unittest.main()
