from __future__ import annotations

import json
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Mapping, Optional, Sequence, Tuple

from localizer.infrastructure.atomic_io import AtomicIO


@dataclass(frozen=True)
class ArtifactFile:
    relative_path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class ReleaseBundle:
    artifact: Path
    manifest: Path
    artifact_sha256: str
    run_id: str
    project_id: str
    mode: str = "release"
    quality_gate_passed: bool = True
    version: str = ""
    variant: str = ""
    release_slug: str = ""
    encryption: str = "none"
    public_metadata: Optional[Path] = None
    public_metadata_sha256: str = ""
    release_name: str = ""
    release_body: str = ""
    created_at: str = ""

    @classmethod
    def load(cls, manifest_path: Path) -> "ReleaseBundle":
        manifest = Path(manifest_path).resolve(strict=True)
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        if raw.get("schema_version") != 1:
            raise ValueError("unsupported artifact manifest schema")
        artifact_name = raw["artifact"]["name"]
        if not isinstance(artifact_name, str) or Path(artifact_name).name != artifact_name:
            raise ValueError("artifact manifest name must be a basename")
        artifact = (manifest.parent / artifact_name).resolve(strict=True)
        public_descriptor = raw.get("public_metadata")
        public_metadata = None
        public_metadata_sha256 = ""
        if public_descriptor is not None:
            if not isinstance(public_descriptor, Mapping):
                raise ValueError("artifact public_metadata must be an object")
            public_name = public_descriptor.get("name")
            if not isinstance(public_name, str) or Path(public_name).name != public_name:
                raise ValueError("artifact public metadata name must be a basename")
            public_metadata = (manifest.parent / public_name).resolve(strict=True)
            public_metadata_sha256 = str(public_descriptor.get("sha256", ""))
        release = raw.get("release", {})
        return cls(
            artifact=artifact,
            manifest=manifest,
            artifact_sha256=raw["artifact"]["sha256"],
            run_id=raw["run_id"],
            project_id=raw["project_id"],
            mode=raw["mode"],
            quality_gate_passed=bool(raw["quality_gate_passed"]),
            version=str(
                release.get("version", raw.get("game_version", ""))
            ),
            variant=str(release.get("variant", "")),
            release_slug=str(release.get("slug", "")),
            encryption=str(raw.get("artifact", {}).get("encryption", "none")),
            public_metadata=public_metadata,
            public_metadata_sha256=public_metadata_sha256,
            release_name=str(release.get("name", "")),
            release_body=str(release.get("body", "")),
            created_at=str(raw.get("created_at", "")),
        )

    def verify(self) -> None:
        if self.mode != "release" or not self.quality_gate_passed:
            raise ValueError("publisher requires a QualityGate-passed release bundle")
        digest = sha256(self.artifact.read_bytes()).hexdigest()
        if digest != self.artifact_sha256:
            raise ValueError("artifact digest does not match manifest")
        if self.public_metadata is not None:
            metadata_bytes = self.public_metadata.read_bytes()
            metadata_digest = sha256(metadata_bytes).hexdigest()
            if (
                not self.public_metadata_sha256
                or metadata_digest != self.public_metadata_sha256
            ):
                raise ValueError("public metadata digest does not match manifest")
            try:
                public = json.loads(metadata_bytes.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("public metadata is not valid UTF-8 JSON") from exc
            required = {"env", "version", "timestamp", "archive", "sha256"}
            if not isinstance(public, Mapping) or set(public) != required:
                raise ValueError("legacy v6 public metadata schema is invalid")
            if public.get("archive") != self.artifact.name:
                raise ValueError("public metadata names a different artifact")
            if public.get("sha256") != self.artifact_sha256:
                raise ValueError("public metadata artifact digest is invalid")
            if self.version and str(public.get("version")) != self.version:
                raise ValueError("public metadata version differs from release identity")


# 发布说明里出现的批次状态，以及它们的中文标签。顺序固定，缺项不打印 ——
# 说明是给人读的，一行 "缩批: 0" 只会稀释真正重要的那几行。
_BATCH_LABELS = (
    ("planned", "批次总数"),
    ("succeeded", "成功批次"),
    ("split_required", "缩批次数"),
    ("retryable", "同尺寸重试"),
    ("failed", "失败批次"),
)


def _batch_lines(summary) -> list:
    """批次概览。`summary` 为空（零 Provider 的重建）时不打印任何行。"""
    if not isinstance(summary, Mapping) or not summary.get("planned"):
        return []
    lines = []
    for key, label in _BATCH_LABELS:
        value = int(summary.get(key, 0) or 0)
        if value:
            lines.append(f"{label}: {value}")
    largest = int(summary.get("largest_batch", 0) or 0)
    smallest = int(summary.get("smallest_batch", 0) or 0)
    if largest:
        # 最大/最小同时出现才有信息量：二者不等就说明这轮确实缩过批。
        lines.append(
            f"批次规模: {smallest}–{largest} 词条"
            if smallest != largest
            else f"批次规模: {largest} 词条"
        )
    return lines


def _lineage_lines(rebuild) -> list:
    """增量谱系。普通新运行没有 rebuild 段，什么都不打印。"""
    if not isinstance(rebuild, Mapping):
        return []
    lines = []
    lineage = rebuild.get("lineage")
    if isinstance(lineage, Sequence) and not isinstance(lineage, (str, bytes)):
        chain = [str(item) for item in lineage]
        if chain:
            lines.append("增量谱系: " + " ← ".join(chain))
    reused = int(rebuild.get("reused", 0) or 0)
    retried = int(rebuild.get("retried", 0) or 0)
    resolved = int(rebuild.get("resolved_by_human", 0) or 0)
    lines.append(f"复用父运行译文: {reused} 条 · 重试: {retried} 条 · 人工定稿: {resolved} 条")
    return lines


class ArtifactBuilder:
    def build_release(
        self,
        *,
        project_id: str,
        run_id: str,
        resource_root: Path,
        resource_paths: Sequence[Path],
        destination: Path,
        manifest_metadata: Optional[Mapping[str, object]] = None,
        version: Optional[str] = None,
        variant: Optional[str] = None,
        artifact_prefix: str = "i18n",
        compression: str = "deflate",
        encryption: str = "none",
        password_env: Optional[str] = None,
        archive_root: Optional[str] = None,
        compatibility_metadata: Optional[Mapping[str, object]] = None,
    ) -> ReleaseBundle:
        component = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
        for field, value in (
            ("version", version),
            ("variant", variant),
            ("artifact_prefix", artifact_prefix),
        ):
            if value is not None and not component.fullmatch(value):
                raise ValueError(f"{field} is not safe for release paths")
        if version and re.match(r"^[vV]\d", version):
            raise ValueError("version must not start with 'v'; release naming adds it")
        normalized_archive_root = self._safe_archive_root(archive_root)
        root = Path(resource_root).resolve(strict=True)
        output = Path(destination).resolve()
        output.mkdir(parents=True, exist_ok=True)
        files = self._inventory(root, resource_paths)
        reserved = {
            "schema_version",
            "project_id",
            "run_id",
            "mode",
            "quality_gate_passed",
            "created_at",
            "artifact",
            "files",
            "release",
            "public_metadata",
        }
        metadata = dict(manifest_metadata or {})
        collisions = reserved & set(metadata)
        if collisions:
            raise ValueError(
                "manifest metadata cannot override reserved fields: "
                + ", ".join(sorted(collisions))
            )
        release_slug = f"{variant}-v{version}" if version and variant else ""
        artifact_name = (
            f"{artifact_prefix}_{variant}_v{version}.zip"
            if version and variant
            else f"{project_id}-{run_id}.zip"
        )
        artifact = output / artifact_name
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{artifact.name}.{run_id}.", suffix=".tmp", dir=str(output)
        )
        os.close(descriptor)
        temp = Path(temp_name)
        try:
            self._write_archive(
                temp,
                root,
                files,
                compression=compression,
                encryption=encryption,
                password_env=password_env,
                archive_root=normalized_archive_root,
            )
            AtomicIO.replace_file(temp, artifact)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
        artifact_digest = sha256(artifact.read_bytes()).hexdigest()
        created_at = datetime.now(timezone.utc).isoformat()
        public_metadata = None
        public_metadata_digest = ""
        compatibility = dict(compatibility_metadata or {})
        if compatibility.get("enabled"):
            if compatibility.get("format", "legacy_v6") != "legacy_v6":
                raise ValueError("unsupported compatibility metadata format")
            if not version or not variant:
                raise ValueError("compatibility metadata requires release identity")
            public_name = str(compatibility.get("filename") or "metadata.json")
            if Path(public_name).name != public_name or not component.fullmatch(public_name):
                raise ValueError("compatibility metadata filename must be a safe basename")
            environment = str(compatibility.get("env") or "").strip()
            if not environment:
                raise ValueError("compatibility metadata requires env")
            if environment.casefold() != variant.casefold():
                raise ValueError(
                    "compatibility metadata env must match the release variant"
                )
            public_metadata = output / public_name
            public_payload = {
                "env": environment,
                "version": version,
                "timestamp": datetime.now().strftime("%Y%m%d_%H%M%S"),
                "archive": artifact.name,
                "sha256": artifact_digest,
            }
            # Keep the v6 key order as well as its schema; old clients parse JSON,
            # while byte-level mirrors remain familiar to operators.
            AtomicIO.write_text(
                public_metadata,
                json.dumps(public_payload, indent=2, ensure_ascii=False),
            )
            public_metadata_digest = sha256(public_metadata.read_bytes()).hexdigest()

        release_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        metrics = metadata.get("translation_metrics")
        metrics = metrics if isinstance(metrics, Mapping) else {}
        release_name = f"汉化自动发布 {release_timestamp}" if release_slug else ""
        release_body = ""
        if release_slug:
            total_tokens = int(metrics.get("input_tokens", 0) or 0) + int(
                metrics.get("output_tokens", 0) or 0
            )
            body_lines = [
                f"发布完成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"API 调用: {int(metrics.get('requests', 0) or 0)} 次",
                f"翻译条数: {int(metrics.get('translation_units_total', 0) or 0)}",
            ]
            translation_files = int(metrics.get("translation_files_total", 0) or 0)
            if translation_files:
                # 这是贡献 Provider 执行涉及的资源集合，不宣称是 ZIP/artifact diff。
                body_lines.append(f"涉及翻译资源: {translation_files} 个")
            body_lines.append(f"消耗 token: {total_tokens}")
            # 批次概览与增量谱系（M6）。此前说明里只有三个总量，回答不了
            # 「这轮有没有缩过批」和「这个包基于哪几轮的钱」——2026-08-04
            # 那次 97/98 个失败全部来自一个 97 词条批次撞读超时，而发布说明
            # 里完全看不出曾经发生过缩批。
            body_lines.extend(_batch_lines(metadata.get("batch_summary")))
            body_lines.extend(_lineage_lines(metadata.get("rebuild")))
            release_body = "\n".join(body_lines)
        manifest = output / f"{artifact.stem}.manifest.json"
        manifest_payload = {
            "schema_version": 1,
            "project_id": project_id,
            "run_id": run_id,
            "mode": "release",
            "quality_gate_passed": True,
            "created_at": created_at,
            "artifact": {
                "name": artifact.name,
                "sha256": artifact_digest,
                "size": artifact.stat().st_size,
                "compression": compression,
                "encryption": encryption,
                "archive_root": normalized_archive_root or "",
            },
            "release": {
                "version": version or "",
                "variant": variant if version and variant else "",
                "slug": release_slug,
                "name": release_name,
                "body": release_body,
            },
            "files": [item.__dict__ for item in files],
        }
        if public_metadata is not None:
            manifest_payload["public_metadata"] = {
                "name": public_metadata.name,
                "sha256": public_metadata_digest,
                "size": public_metadata.stat().st_size,
                "format": "legacy_v6",
            }
        manifest_payload.update(metadata)
        AtomicIO.write_json(manifest, manifest_payload)
        bundle = ReleaseBundle(
            artifact,
            manifest,
            artifact_digest,
            run_id,
            project_id,
            version=version or "",
            variant=variant if version and variant else "",
            release_slug=release_slug,
            encryption=encryption,
            public_metadata=public_metadata,
            public_metadata_sha256=public_metadata_digest,
            release_name=release_name,
            release_body=release_body,
            created_at=created_at,
        )
        bundle.verify()
        return bundle

    @staticmethod
    def _write_archive(
        destination: Path,
        root: Path,
        files: Sequence[ArtifactFile],
        *,
        compression: str,
        encryption: str,
        password_env: Optional[str],
        archive_root: Optional[str],
    ) -> None:
        compression_types = {
            "stored": zipfile.ZIP_STORED,
            "deflate": zipfile.ZIP_DEFLATED,
            "lzma": zipfile.ZIP_LZMA,
        }
        try:
            compression_type = compression_types[compression]
        except KeyError as exc:
            raise ValueError(f"unsupported zip compression: {compression}") from exc

        archive_type = zipfile.ZipFile
        password: Optional[bytes] = None
        encryption_kwargs = {}
        encryption_method = None
        if encryption == "aes256":
            if not password_env or not os.environ.get(password_env):
                raise ValueError("archive password environment variable is unset")
            try:
                import pyzipper
            except ImportError as exc:
                raise RuntimeError(
                    "AES archive creation requires the 'artifact-aes' optional dependency"
                ) from exc
            archive_type = pyzipper.AESZipFile
            compression_type = {
                "stored": pyzipper.ZIP_STORED,
                "deflate": pyzipper.ZIP_DEFLATED,
                "lzma": pyzipper.ZIP_LZMA,
            }[compression]
            password = os.environ[password_env].encode("utf-8")
            encryption_kwargs = {"encryption": pyzipper.WZ_AES}
            encryption_method = pyzipper.WZ_AES
        elif encryption != "none":
            raise ValueError(f"unsupported zip encryption: {encryption}")

        with archive_type(
            destination, "w", compression=compression_type, **encryption_kwargs
        ) as archive:
            if password is not None:
                archive.setpassword(password)
                archive.setencryption(encryption_method, nbits=256)
            for item in files:
                source = root / item.relative_path
                archive_path = "/".join(
                    part for part in (archive_root, item.relative_path) if part
                )
                if encryption == "none":
                    info = zipfile.ZipInfo(archive_path)
                    info.date_time = (1980, 1, 1, 0, 0, 0)
                    info.compress_type = compression_type
                    archive.writestr(info, source.read_bytes())
                else:
                    # pyzipper 维护自己的 ZipInfo 派生类型；直接传归档路径最兼容。
                    # AES 自带随机 salt，本来也不承诺字节级可复现。
                    archive.writestr(archive_path, source.read_bytes())

    @staticmethod
    def _safe_archive_root(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        candidate = value.replace("\\", "/").strip("/")
        parts = candidate.split("/") if candidate else []
        component = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
        if not parts or any(
            part in {"", ".", ".."} or not component.fullmatch(part)
            for part in parts
        ):
            raise ValueError("archive_root must be a safe relative archive path")
        return "/".join(parts)

    @staticmethod
    def _inventory(root: Path, resource_paths: Sequence[Path]) -> Tuple[ArtifactFile, ...]:
        items = []
        seen = set()
        for path in resource_paths:
            candidate = Path(path).resolve(strict=True)
            try:
                relative = candidate.relative_to(root).as_posix()
            except ValueError as exc:
                raise ValueError(f"artifact resource is outside root: {candidate}") from exc
            if relative in seen:
                raise ValueError(f"duplicate artifact path: {relative}")
            seen.add(relative)
            content = candidate.read_bytes()
            items.append(ArtifactFile(relative, sha256(content).hexdigest(), len(content)))
        return tuple(sorted(items, key=lambda item: item.relative_path))