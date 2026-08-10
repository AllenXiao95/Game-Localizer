from __future__ import annotations

from hashlib import sha256
import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Mapping, Optional, Protocol

from localizer.adapters.publishers.common import verified_payloads
from localizer.application.artifact import ReleaseBundle
from localizer.ports.publisher import PublishedObject, PublishReceipt


class GitHubReleaseClient(Protocol):
    def ensure_release(
        self, repository: str, tag: str, *, name: str = "", body: str = ""
    ) -> str:
        """Return an opaque release id."""

    def download_asset(self, release_id: str, name: str) -> Optional[bytes]:
        ...

    def upload_asset(
        self, release_id: str, name: str, content: bytes, content_type: str
    ) -> str:
        """Return the published object locator."""


class GitHubReleasePublisher:
    def __init__(
        self, client: GitHubReleaseClient, *, repository: str, tag: Optional[str] = None
    ) -> None:
        self.client = client
        self.repository = repository
        self.tag = tag

    def publish(self, bundle: ReleaseBundle) -> PublishReceipt:
        tag = self.tag or bundle.release_slug
        if not tag:
            raise ValueError("GitHub release tag is not configured and cannot be derived")
        release_id = self.client.ensure_release(
            self.repository,
            tag,
            name=bundle.release_name or tag,
            body=bundle.release_body,
        )
        objects = []
        for name, content, digest in verified_payloads(bundle):
            existing = self.client.download_asset(release_id, name)
            skipped = False
            if existing is not None:
                if sha256(existing).hexdigest() != digest:
                    raise FileExistsError(
                        f"GitHub release asset exists with different bytes: {name}"
                    )
                locator = f"github://{self.repository}/{tag}/{name}"
                skipped = True
            else:
                locator = self.client.upload_asset(
                    release_id,
                    name,
                    content,
                    mimetypes.guess_type(name)[0] or "application/octet-stream",
                )
            downloaded = self.client.download_asset(release_id, name)
            if downloaded is None or sha256(downloaded).hexdigest() != digest:
                raise IOError(f"GitHub release upload verification failed: {name}")
            objects.append(PublishedObject(name, locator, digest, len(content), skipped))
        return PublishReceipt("github_release", tuple(objects))


class GitHubAPIError(RuntimeError):
    pass


class UrllibGitHubReleaseClient:
    """GitHub REST client; constructing it does not perform network I/O."""

    def __init__(
        self,
        *,
        token_env: str,
        api_url: str = "https://api.github.com",
        upload_url: str = "https://uploads.github.com",
        timeout_seconds: float = 120,
    ) -> None:
        token = os.environ.get(token_env)
        if not token:
            raise ValueError(
                "GitHub token environment variable is unset"
            )  # 不回显变量名：它可能被误填成真 PAT，异常会进 CI 日志
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.upload_url = upload_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.release_repositories: Dict[str, str] = {}

    def ensure_release(
        self, repository: str, tag: str, *, name: str = "", body: str = ""
    ) -> str:
        encoded_repository = urllib.parse.quote(repository, safe="/")
        encoded_tag = urllib.parse.quote(tag, safe="")
        status, payload = self._request(
            "GET", f"{self.api_url}/repos/{encoded_repository}/releases/tags/{encoded_tag}"
        )
        if status == 404:
            status, payload = self._request(
                "POST",
                f"{self.api_url}/repos/{encoded_repository}/releases",
                {
                    "tag_name": tag,
                    "name": name or tag,
                    "body": body,
                    "draft": False,
                    "prerelease": False,
                },
            )
        if status not in {200, 201} or not isinstance(payload, Mapping) or "id" not in payload:
            raise GitHubAPIError(f"cannot ensure GitHub release for {repository}@{tag}")
        release_id = str(payload["id"])
        self.release_repositories[release_id] = repository
        return release_id

    def download_asset(self, release_id: str, name: str) -> Optional[bytes]:
        repository = self._repository(release_id)
        encoded = urllib.parse.quote(repository, safe="/")
        status, payload = self._request(
            "GET", f"{self.api_url}/repos/{encoded}/releases/{release_id}/assets"
        )
        if status != 200 or not isinstance(payload, list):
            raise GitHubAPIError(f"cannot list assets for release {release_id}")
        asset_url = None
        for item in payload:
            if isinstance(item, Mapping) and item.get("name") == name:
                asset_url = item.get("url")
                break
        if not isinstance(asset_url, str):
            return None
        status, content = self._request_bytes(
            "GET", asset_url, accept="application/octet-stream"
        )
        if status != 200:
            raise GitHubAPIError(f"cannot download GitHub asset {name}")
        return content

    def upload_asset(
        self, release_id: str, name: str, content: bytes, content_type: str
    ) -> str:
        repository = self._repository(release_id)
        encoded = urllib.parse.quote(repository, safe="/")
        query = urllib.parse.urlencode({"name": name})
        status, payload = self._request_bytes(
            "POST",
            f"{self.upload_url}/repos/{encoded}/releases/{release_id}/assets?{query}",
            body=content,
            content_type=content_type,
        )
        if status != 201:
            raise GitHubAPIError(f"cannot upload GitHub asset {name}")
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise GitHubAPIError("GitHub upload returned invalid JSON") from exc
        return str(decoded.get("browser_download_url", decoded.get("url", name)))

    def _repository(self, release_id: str) -> str:
        try:
            return self.release_repositories[release_id]
        except KeyError as exc:
            raise GitHubAPIError(f"unknown release id: {release_id}") from exc

    def _request(
        self, method: str, url: str, payload: Optional[Mapping[str, object]] = None
    ):
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        status, content = self._request_bytes(
            method, url, body=body, content_type="application/json"
        )
        if status == 404:
            return status, None
        try:
            return status, json.loads(content.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise GitHubAPIError(f"GitHub returned invalid JSON for {url}") from exc

    def _request_bytes(
        self,
        method: str,
        url: str,
        *,
        body: Optional[bytes] = None,
        content_type: Optional[str] = None,
        accept: str = "application/vnd.github+json",
    ):
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "game-localizer",
        }
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return 404, exc.read()
            raise GitHubAPIError(f"GitHub HTTP {exc.code}: {url}") from exc
        except urllib.error.URLError as exc:
            raise GitHubAPIError(f"GitHub connection failed: {exc}") from exc
