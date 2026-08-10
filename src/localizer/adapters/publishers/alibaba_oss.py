from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import mimetypes
import os
from typing import Callable, Mapping, Optional, Protocol
import urllib.error
import urllib.request

from localizer.adapters.publishers.common import verified_payloads
from localizer.application.artifact import ReleaseBundle
from localizer.ports.publisher import PublishedObject, PublishReceipt


@dataclass(frozen=True)
class OSSSTSCredentials:
    access_key_id: str
    access_key_secret: str
    security_token: str


STSTransport = Callable[[str, Mapping[str, str], float], Mapping[str, object]]


class OSSSTSCredentialProvider:
    """Fetch short-lived OSS credentials from the legacy-compatible STS endpoint.

    The configured token is always an environment-variable reference.  The
    provider deliberately does not put the variable name or its value in errors,
    because those messages may be copied into CI logs.
    """

    def __init__(
        self,
        *,
        url: str,
        token_env: str,
        token_header: str = "token",
        timeout_seconds: float = 10,
        transport: Optional[STSTransport] = None,
    ) -> None:
        token = os.environ.get(token_env)
        if not token:
            raise ValueError("OSS STS API credential environment variable is unset")
        self.url = url
        self.token = token
        self.token_header = token_header
        self.timeout_seconds = timeout_seconds
        self.transport = transport or self._urlopen_transport

    def fetch(self) -> OSSSTSCredentials:
        raw = self.transport(
            self.url, {self.token_header: self.token}, self.timeout_seconds
        )
        # Some STS gateways return the credentials directly, while others wrap
        # them in a Credentials/data object.  Accept these common shapes without
        # making the publisher dependent on one deployment's response envelope.
        payload: object = raw
        for key in ("Credentials", "credentials", "data"):
            nested = raw.get(key)
            if isinstance(nested, Mapping):
                payload = nested
                break
        if not isinstance(payload, Mapping):
            raise ValueError("OSS STS API returned an invalid credential payload")
        try:
            access_key_id = payload["AccessKeyId"]
            access_key_secret = payload["AccessKeySecret"]
            security_token = payload["SecurityToken"]
        except KeyError as exc:
            raise ValueError("OSS STS API response is missing required fields") from exc
        if not all(
            isinstance(value, str) and value
            for value in (access_key_id, access_key_secret, security_token)
        ):
            raise ValueError("OSS STS API returned invalid credential fields")
        return OSSSTSCredentials(access_key_id, access_key_secret, security_token)

    @staticmethod
    def _urlopen_transport(
        url: str, headers: Mapping[str, str], timeout: float
    ) -> Mapping[str, object]:
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise ConnectionError(f"OSS STS API HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise ConnectionError("OSS STS API connection failed") from exc
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("OSS STS API returned invalid JSON") from exc
        if not isinstance(decoded, Mapping):
            raise ValueError("OSS STS API response root must be an object")
        return decoded


class OSSClient(Protocol):
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
        ...

    def get_object(self, bucket: str, key: str) -> bytes:
        ...


class AlibabaOSSPublisher:
    def __init__(self, client: OSSClient, *, bucket: str, prefix: str = "",
                 versioned_prefix: bool = False) -> None:
        self.client = client
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.versioned_prefix = versioned_prefix

    def publish(self, bundle: ReleaseBundle) -> PublishReceipt:
        prefix = self.prefix
        if self.versioned_prefix:
            if not bundle.release_slug:
                raise ValueError("versioned OSS prefix requires release identity")
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
                    # Legacy v6 objects have no custom digest metadata.
                    matches = sha256(
                        self.client.get_object(self.bucket, key)
                    ).hexdigest() == digest
                if not matches:
                    raise FileExistsError(
                        f"OSS object exists with a different digest: {self.bucket}/{key}"
                    )
                locator = f"oss://{self.bucket}/{key}"
                skipped = True
            else:
                content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
                locator = self.client.put_object(
                    self.bucket,
                    key,
                    content,
                    {"sha256": digest},
                    content_type,
                )
            downloaded = self.client.get_object(self.bucket, key)
            if sha256(downloaded).hexdigest() != digest:
                raise IOError(f"OSS upload verification failed: {self.bucket}/{key}")
            objects.append(PublishedObject(name, locator, digest, len(content), skipped))
        return PublishReceipt("alibaba_oss", tuple(objects))


class OSS2STSClient:
    """Lazy oss2 client backed by short-lived STS credentials."""

    def __init__(self, *, endpoint: str, credentials: OSSSTSCredentialProvider) -> None:
        self.endpoint = endpoint
        self.credentials = credentials
        self._buckets = {}

    def _bucket(self, bucket_name: str):
        bucket = self._buckets.get(bucket_name)
        if bucket is not None:
            return bucket
        try:
            import oss2
        except ImportError as exc:
            raise RuntimeError(
                "OSS publishing requires the 'publish-oss' optional dependency"
            ) from exc
        sts = self.credentials.fetch()
        auth = oss2.StsAuth(
            sts.access_key_id, sts.access_key_secret, sts.security_token
        )
        bucket = oss2.Bucket(auth, self.endpoint, bucket_name)
        self._buckets[bucket_name] = bucket
        return bucket

    def head_object(self, bucket: str, key: str) -> Optional[Mapping[str, str]]:
        try:
            result = self._bucket(bucket).head_object(key)
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                return None
            raise
        headers = {
            str(name).lower(): str(value)
            for name, value in dict(getattr(result, "headers", {})).items()
        }
        digest = headers.get("x-oss-meta-sha256")
        return {"sha256": digest} if digest else {}

    def put_object(
        self,
        bucket: str,
        key: str,
        content: bytes,
        metadata: Mapping[str, str],
        content_type: str,
    ) -> str:
        headers = {"Content-Type": content_type}
        headers.update({f"x-oss-meta-{name}": value for name, value in metadata.items()})
        self._bucket(bucket).put_object(key, content, headers=headers)
        return f"oss://{bucket}/{key}"

    def get_object(self, bucket: str, key: str) -> bytes:
        return self._bucket(bucket).get_object(key).read()
