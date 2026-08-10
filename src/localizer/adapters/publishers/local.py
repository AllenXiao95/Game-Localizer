from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from localizer.adapters.publishers.common import verified_payloads
from localizer.application.artifact import ReleaseBundle
from localizer.infrastructure.atomic_io import AtomicIO
from localizer.ports.publisher import PublishedObject, PublishReceipt


class LocalPublisher:
    def __init__(
        self, destination: Path, *, versioned_prefix: bool = False
    ) -> None:
        self.destination = Path(destination).resolve()
        self.versioned_prefix = versioned_prefix

    def publish(self, bundle: ReleaseBundle) -> PublishReceipt:
        destination = self.destination
        if self.versioned_prefix:
            if not bundle.release_slug:
                raise ValueError("versioned local destination requires release identity")
            destination = destination / bundle.release_slug
        destination.mkdir(parents=True, exist_ok=True)
        published = []
        for name, content, digest in verified_payloads(
            bundle, include_internal_manifest=True
        ):
            target = destination / name
            skipped = False
            if target.exists():
                existing = sha256(target.read_bytes()).hexdigest()
                if existing != digest:
                    raise FileExistsError(
                        f"refusing to overwrite published object with different bytes: {target}"
                    )
                skipped = True
            else:
                AtomicIO.write_bytes(target, content)
            verified = sha256(target.read_bytes()).hexdigest()
            if verified != digest:
                raise IOError(f"published object verification failed: {target}")
            published.append(
                PublishedObject(name, str(target), digest, len(content), skipped)
            )
        return PublishReceipt("local", tuple(published))
