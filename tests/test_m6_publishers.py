from __future__ import annotations

import sys
import json
import importlib.util
import os
import tempfile
import unittest
import zipfile
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from localizer.adapters.publishers.cloudflare_r2 import CloudflareR2Publisher
from localizer.adapters.publishers.github_release import GitHubReleasePublisher
from localizer.adapters.publishers.local import LocalPublisher
from localizer.adapters.publishers.alibaba_oss import (
    AlibabaOSSPublisher,
    OSSSTSCredentialProvider,
)
from localizer.application.artifact import ArtifactBuilder, ReleaseBundle
from localizer.application.publish import publisher_from_config
from localizer.config.models import PublishTargetSection


class FakeGitHub:
    def __init__(self):
        self.assets = {}
        self.uploads = 0
        self.releases = {}
        self.content_types = {}

    def ensure_release(self, repository, tag, *, name="", body=""):
        self.releases[(repository, tag)] = {"name": name, "body": body}
        return f"{repository}:{tag}"

    def download_asset(self, release_id, name):
        return self.assets.get((release_id, name))

    def upload_asset(self, release_id, name, content, content_type):
        self.uploads += 1
        self.assets[(release_id, name)] = content
        self.content_types[(release_id, name)] = content_type
        return f"https://github.invalid/{release_id}/{name}"


class FakeR2:
    def __init__(self):
        self.objects = {}
        self.metadata = {}
        self.content_types = {}
        self.uploads = 0

    def head_object(self, bucket, key):
        return self.metadata.get((bucket, key))

    def put_object(self, bucket, key, content, metadata, content_type):
        self.uploads += 1
        self.objects[(bucket, key)] = content
        self.metadata[(bucket, key)] = dict(metadata)
        self.content_types[(bucket, key)] = content_type
        return f"r2://{bucket}/{key}"

    def get_object(self, bucket, key):
        return self.objects[(bucket, key)]


class FakeOSS:
    def __init__(self):
        self.objects = {}
        self.metadata = {}
        self.content_types = {}
        self.uploads = 0

    def head_object(self, bucket, key):
        return self.metadata.get((bucket, key))

    def put_object(self, bucket, key, content, metadata, content_type):
        self.uploads += 1
        self.objects[(bucket, key)] = content
        self.metadata[(bucket, key)] = dict(metadata)
        self.content_types[(bucket, key)] = content_type
        return f"oss://{bucket}/{key}"

    def get_object(self, bucket, key):
        return self.objects[(bucket, key)]


class PublisherTests(unittest.TestCase):
    def bundle(self, root: Path) -> ReleaseBundle:
        resources = root / "resources"
        resources.mkdir()
        first = resources / "a.txt"
        first.write_text("artifact", encoding="utf-8")
        return ArtifactBuilder().build_release(
            project_id="test",
            run_id="run-1",
            resource_root=resources,
            resource_paths=[first],
            destination=root / "bundle",
        )

    def test_local_publish_is_verified_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = self.bundle(root)
            publisher = LocalPublisher(root / "published")
            first = publisher.publish(bundle)
            second = publisher.publish(bundle)
            self.assertTrue(all(not item.skipped for item in first.objects))
            self.assertTrue(all(item.skipped for item in second.objects))

    def test_versioned_local_publish_isolates_fixed_public_metadata_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            resources = root / "resources"
            resources.mkdir()
            source = resources / "a.txt"
            source.write_text("artifact", encoding="utf-8")
            bundle = ArtifactBuilder().build_release(
                project_id="wot",
                run_id="run-1",
                resource_root=resources,
                resource_paths=[source],
                destination=root / "bundle",
                version="1.44.0.0",
                variant="ru",
                compatibility_metadata={
                    "enabled": True,
                    "format": "legacy_v6",
                    "filename": "metadata.json",
                    "env": "RU",
                },
            )
            LocalPublisher(
                root / "published", versioned_prefix=True
            ).publish(bundle)
            version_root = root / "published" / "ru-v1.44.0.0"
            self.assertTrue((version_root / bundle.artifact.name).is_file())
            self.assertTrue((version_root / "metadata.json").is_file())
            self.assertTrue((version_root / bundle.manifest.name).is_file())

    def test_publisher_factory_requires_environment_references_and_target_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            publisher = publisher_from_config(
                PublishTargetSection(type="local", destination=Path(temp))
            )
            self.assertIsInstance(publisher, LocalPublisher)
        with self.assertRaises(Exception):
            PublishTargetSection(
                type="github_release",
                repository="owner/repo",
                tag="v1",
                token_env="github-token-value",
            )
        with self.assertRaises(ValueError):
            publisher_from_config(PublishTargetSection(type="github_release"))

    def test_publishers_reject_preview_or_tampered_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bundle = self.bundle(root)
            preview = ReleaseBundle(
                bundle.artifact,
                bundle.manifest,
                bundle.artifact_sha256,
                bundle.run_id,
                bundle.project_id,
                mode="preview",
                quality_gate_passed=False,
            )
            with self.assertRaises(ValueError):
                LocalPublisher(root / "published").publish(preview)
            bundle.artifact.write_bytes(b"tampered")
            with self.assertRaises(ValueError):
                LocalPublisher(root / "published").publish(bundle)

    def test_github_release_uploads_and_verifies_then_skips(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bundle = self.bundle(Path(temp))
            client = FakeGitHub()
            publisher = GitHubReleasePublisher(
                client, repository="owner/repo", tag="v1"
            )
            publisher.publish(bundle)
            publisher.publish(bundle)
            self.assertEqual(2, client.uploads)

    def test_version_drives_artifact_tag_and_object_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            resources = root / "resources"
            resources.mkdir()
            source = resources / "a.txt"
            source.write_text("artifact", encoding="utf-8")
            bundle = ArtifactBuilder().build_release(
                project_id="wot",
                run_id="release-1",
                resource_root=resources,
                resource_paths=[source],
                destination=root / "bundle",
                version="1.44.0.0",
                variant="ru",
                artifact_prefix="i18n",
                archive_root="lc_messages",
                compatibility_metadata={
                    "enabled": True,
                    "format": "legacy_v6",
                    "filename": "metadata.json",
                    "env": "RU",
                },
            )
            self.assertEqual("i18n_ru_v1.44.0.0.zip", bundle.artifact.name)
            manifest = json.loads(bundle.manifest.read_text(encoding="utf-8"))
            self.assertEqual("ru-v1.44.0.0", manifest["release"]["slug"])
            self.assertTrue(manifest["release"]["name"].startswith("汉化自动发布 "))
            self.assertEqual("lc_messages", manifest["artifact"]["archive_root"])
            metadata = json.loads(bundle.public_metadata.read_text(encoding="utf-8"))
            self.assertEqual(
                {"env", "version", "timestamp", "archive", "sha256"},
                set(metadata),
            )
            self.assertEqual(bundle.artifact.name, metadata["archive"])
            self.assertEqual(bundle.artifact_sha256, metadata["sha256"])
            with zipfile.ZipFile(bundle.artifact) as archive:
                self.assertEqual(["lc_messages/a.txt"], archive.namelist())

            github = FakeGitHub()
            GitHubReleasePublisher(
                github, repository="owner/repo"
            ).publish(bundle)
            self.assertTrue(
                all(key[0] == "owner/repo:ru-v1.44.0.0" for key in github.assets)
            )
            self.assertEqual(
                {"i18n_ru_v1.44.0.0.zip", "metadata.json"},
                {key[1] for key in github.assets},
            )
            self.assertEqual(
                "application/json",
                next(
                    value for key, value in github.content_types.items()
                    if key[1] == "metadata.json"
                ),
            )
            self.assertTrue(
                github.releases[("owner/repo", "ru-v1.44.0.0")]["name"].startswith(
                    "汉化自动发布 "
                )
            )

            r2 = FakeR2()
            CloudflareR2Publisher(
                r2, bucket="translations", prefix="wot", versioned_prefix=True
            ).publish(bundle)
            self.assertTrue(
                all(key[1].startswith("wot/ru-v1.44.0.0/") for key in r2.objects)
            )
            self.assertEqual("application/json", next(
                value for key, value in r2.content_types.items()
                if key[1].endswith("metadata.json")
            ))

            oss = FakeOSS()
            AlibabaOSSPublisher(
                oss,
                bucket="translations",
                prefix="tankBox_ru/hanhua",
                versioned_prefix=True,
            ).publish(bundle)
            self.assertTrue(
                all(
                    key[1].startswith("tankBox_ru/hanhua/ru-v1.44.0.0/")
                    for key in oss.objects
                )
            )

    def test_version_with_leading_v_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            resources = root / "resources"
            resources.mkdir()
            source = resources / "a.txt"
            source.write_text("artifact", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must not start with 'v'"):
                ArtifactBuilder().build_release(
                    project_id="wot",
                    run_id="release-1",
                    resource_root=resources,
                    resource_paths=[source],
                    destination=root / "bundle",
                    version="v1.44.0.0",
                    variant="ru",
                )

    def test_aes_archive_requires_password_and_optional_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            resources = root / "resources"
            resources.mkdir()
            source = resources / "a.txt"
            source.write_text("secret artifact", encoding="utf-8")
            kwargs = dict(
                project_id="wot",
                run_id="release-1",
                resource_root=resources,
                resource_paths=[source],
                destination=root / "bundle",
                version="1",
                variant="ru",
                compression="lzma",
                encryption="aes256",
                password_env="ARCHIVE_TEST_PASSWORD",
                archive_root="lc_messages",
                compatibility_metadata={
                    "enabled": True,
                    "format": "legacy_v6",
                    "filename": "metadata.json",
                    "env": "RU",
                },
            )
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "password environment"):
                    ArtifactBuilder().build_release(**kwargs)
            with patch.dict(os.environ, {"ARCHIVE_TEST_PASSWORD": "correct horse"}):
                if importlib.util.find_spec("pyzipper") is None:
                    with self.assertRaisesRegex(RuntimeError, "artifact-aes"):
                        ArtifactBuilder().build_release(**kwargs)
                else:
                    import pyzipper

                    bundle = ArtifactBuilder().build_release(**kwargs)
                    manifest_text = bundle.manifest.read_text(encoding="utf-8")
                    self.assertEqual(
                        "aes256", json.loads(manifest_text)["artifact"]["encryption"]
                    )
                    self.assertNotIn("correct horse", manifest_text)
                    with pyzipper.AESZipFile(bundle.artifact) as archive:
                        archive.setpassword(b"correct horse")
                        info = archive.getinfo("lc_messages/a.txt")
                        self.assertEqual(zipfile.ZIP_LZMA, info.compress_type)
                        self.assertTrue(info.extra.startswith(b"\x01\x99\x07\x00"))
                        self.assertEqual(3, info.extra[8])  # WinZip AES-256 strength
                        self.assertEqual(b"\x0e\x00", info.extra[9:11])  # LZMA
                        self.assertEqual(b"secret artifact", archive.read(info))
                    with pyzipper.AESZipFile(bundle.artifact) as archive:
                        archive.setpassword(b"wrong password")
                        with self.assertRaises(RuntimeError):
                            archive.read("lc_messages/a.txt")

    def test_r2_uploads_with_digest_metadata_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bundle = self.bundle(Path(temp))
            client = FakeR2()
            publisher = CloudflareR2Publisher(
                client, bucket="translations", prefix="wot/v1"
            )
            publisher.publish(bundle)
            publisher.publish(bundle)
            self.assertEqual(2, client.uploads)
            self.assertTrue(
                all("sha256" in value for value in client.metadata.values())
            )
            self.assertIn("application/json", client.content_types.values())

    def test_r2_and_oss_accept_matching_legacy_objects_without_digest_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bundle = self.bundle(Path(temp))
            for client in (FakeR2(), FakeOSS()):
                publisher = (
                    CloudflareR2Publisher(client, bucket="translations")
                    if isinstance(client, FakeR2)
                    else AlibabaOSSPublisher(client, bucket="translations")
                )
                for name, content in (
                    (bundle.artifact.name, bundle.artifact.read_bytes()),
                    (bundle.manifest.name, bundle.manifest.read_bytes()),
                ):
                    client.objects[("translations", name)] = content
                    client.metadata[("translations", name)] = {}
                receipt = publisher.publish(bundle)
                self.assertTrue(all(item.skipped for item in receipt.objects))
                self.assertEqual(0, client.uploads)

    def test_oss_uploads_with_sts_compatible_semantics_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bundle = self.bundle(Path(temp))
            client = FakeOSS()
            publisher = AlibabaOSSPublisher(
                client, bucket="translations", prefix="tankBox_ru/hanhua/ru-v1"
            )
            first = publisher.publish(bundle)
            second = publisher.publish(bundle)
            self.assertEqual(2, client.uploads)  # artifact + manifest, only once each
            self.assertTrue(all(not item.skipped for item in first.objects))
            self.assertTrue(all(item.skipped for item in second.objects))
            self.assertTrue(
                all("sha256" in value for value in client.metadata.values())
            )
            self.assertIn("application/json", client.content_types.values())

    def test_oss_sts_credentials_come_from_an_environment_reference(self) -> None:
        captured = {}

        def transport(url, headers, timeout):
            captured.update({"url": url, "headers": headers, "timeout": timeout})
            return {
                "Credentials": {
                    "AccessKeyId": "synthetic-access-id",
                    "AccessKeySecret": "synthetic-secret",
                    "SecurityToken": "synthetic-session-token",
                }
            }

        with patch.dict("os.environ", {"OSS_API_TOKEN_HEADER": "synthetic-api-token"}):
            provider = OSSSTSCredentialProvider(
                url="https://sts.invalid/token",
                token_env="OSS_API_TOKEN_HEADER",
                token_header="token",
                timeout_seconds=10,
                transport=transport,
            )
            credentials = provider.fetch()
        self.assertEqual("synthetic-access-id", credentials.access_key_id)
        self.assertEqual("synthetic-api-token", captured["headers"]["token"])

    def test_oss_factory_requires_sts_fields_without_echoing_credential(self) -> None:
        with self.assertRaises(ValueError):
            publisher_from_config(PublishTargetSection(type="alibaba_oss"))
        with patch.dict("os.environ", {"OSS_API_TOKEN_HEADER": "synthetic-api-token"}):
            publisher = publisher_from_config(
                PublishTargetSection(
                    type="alibaba_oss",
                    endpoint="https://oss.invalid",
                    bucket="translations",
                    sts_token_url="https://sts.invalid/token",
                    sts_token_env="OSS_API_TOKEN_HEADER",
                )
            )
        self.assertIsInstance(publisher, AlibabaOSSPublisher)
        sentinel = "synthetic-api-token-must-not-appear"
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ValueError) as caught:
                publisher_from_config(
                    PublishTargetSection(
                        type="alibaba_oss",
                        endpoint="https://oss.invalid",
                        bucket="translations",
                        sts_token_url="https://sts.invalid/token",
                        sts_token_env="OSS_API_TOKEN_HEADER",
                    )
                )
        self.assertNotIn("OSS_API_TOKEN_HEADER", str(caught.exception))
        self.assertNotIn(sentinel, str(caught.exception))

    def test_oss_rejects_unsafe_sts_header_names(self) -> None:
        with self.assertRaises(Exception):
            PublishTargetSection(
                type="alibaba_oss",
                sts_token_header="token\r\nX-Injected",
            )


if __name__ == "__main__":
    unittest.main()
