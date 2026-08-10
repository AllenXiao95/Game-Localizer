from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Tuple

from localizer.application.artifact import ReleaseBundle


def verified_payloads(
    bundle: ReleaseBundle, *, include_internal_manifest: bool = False
) -> Tuple[Tuple[str, bytes, str], ...]:
    """Return the public release payloads.

    Legacy-compatible projects publish the archive plus ``metadata.json``.  The
    richer internal manifest remains local unless explicitly requested.  Generic
    projects without a public descriptor retain the previous archive + manifest
    behaviour.
    """
    bundle.verify()
    payloads = []
    paths = [bundle.artifact]
    if bundle.public_metadata is not None:
        paths.append(bundle.public_metadata)
        if include_internal_manifest:
            paths.append(bundle.manifest)
    else:
        paths.append(bundle.manifest)
    for path in paths:
        content = Path(path).read_bytes()
        payloads.append((Path(path).name, content, sha256(content).hexdigest()))
    return tuple(payloads)
