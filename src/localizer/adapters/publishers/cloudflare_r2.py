from __future__ import annotations

from hashlib import sha256
import mimetypes
import os
from typing import Mapping, Optional, Protocol

from localizer.adapters.publishers.common import verified_payloads
from localizer.application.artifact import ReleaseBundle
from localizer.ports.publisher import PublishedObject, PublishReceipt


class R2Client(Protocol):
    def head_object(self, bucket: str, key: str) -> Optional[Mapping[str, str]]:
        ...

    def put_object(
        self,
        bucket: str,
        key: str,
        content: bytes,
        metadata: Mapping[str, str],
        content_type: str,
    ) -> str:
        """Return the uploaded object locator."""

    def get_object(self, bucket: str, key: str) -> bytes:
        ...


class CloudflareR2Publisher:
    def __init__(self, client: R2Client, *, bucket: str, prefix: str = "",
                 versioned_prefix: bool = False) -> None:
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.versioned_prefix = versioned_prefix

    def publish(self, bundle: ReleaseBundle) -> PublishReceipt:
        prefix = self.prefix
        if self.versioned_prefix:
            if not bundle.release_slug:
                raise ValueError("versioned R2 prefix requires release identity")
            prefix = "/".join(part for part in (prefix, bundle.release_slug) if part)
        objects = []
        for name, content, digest in verified_payloads(bundle):
            key = f"{prefix}/{name}" if prefix else name
            metadata = self.client.head_object(self.bucket, key)
            skipped = False
            if metadata is not None:
                recorded_digest = metadata.get("sha256")
                if recorded_digest:
                    matches = recorded_digest == digest
                else:
                    # v6 objects predate custom sha256 metadata.  Compare their
                    # bytes so an exact legacy object is safely idempotent.
                    matches = sha256(
                        self.client.get_object(self.bucket, key)
                    ).hexdigest() == digest
                if not matches:
                    raise FileExistsError(
                        f"R2 object exists with a different digest: {self.bucket}/{key}"
                    )
                locator = f"r2://{self.bucket}/{key}"
                skipped = True
            else:
                content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
                locator = self.client.put_object(
                    self.bucket, key, content, {"sha256": digest}, content_type
                )
            downloaded = self.client.get_object(self.bucket, key)
            if sha256(downloaded).hexdigest() != digest:
                raise IOError(f"R2 upload verification failed: {self.bucket}/{key}")
            objects.append(PublishedObject(name, locator, digest, len(content), skipped))
        return PublishReceipt("cloudflare_r2", tuple(objects))


class Boto3R2Client:
    """S3-compatible Cloudflare R2 client; credentials are environment references only."""

    def __init__(
        self,
        *,
        account_id: Optional[str],
        access_key_env: str,
        secret_key_env: str,
        endpoint_url: Optional[str] = None,
    ) -> None:
        access_key = os.environ.get(access_key_env)
        secret_key = os.environ.get(secret_key_env)
        if not access_key or not secret_key:
            raise ValueError("R2 credential environment variables are unset")
        try:
            import boto3
            from botocore.exceptions import ClientError
        except ImportError as exc:
            raise RuntimeError(
                "R2 publishing requires the 'publish-r2' optional dependency"
            ) from exc
        self.ClientError = ClientError
        if endpoint_url:
            endpoint = endpoint_url
        elif account_id:
            endpoint = f"https://{account_id}.r2.cloudflarestorage.com"
        else:  # Defensive: project config validation normally catches this first.
            raise ValueError("R2 requires account_id or endpoint_url")
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="auto",
        )

    def head_object(self, bucket: str, key: str) -> Optional[Mapping[str, str]]:
        try:
            response = self.client.head_object(Bucket=bucket, Key=key)
        except self.ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        return dict(response.get("Metadata", {}))

    def put_object(
        self,
        bucket: str,
        key: str,
        content: bytes,
        metadata: Mapping[str, str],
        content_type: str,
    ) -> str:
        self.client.put_object(
            Bucket=bucket,
            Key=key,
            Body=content,
            Metadata=dict(metadata),
            ContentType=content_type,
        )
        return f"r2://{bucket}/{key}"

    def get_object(self, bucket: str, key: str) -> bytes:
        response = self.client.get_object(Bucket=bucket, Key=key)
        return response["Body"].read()
