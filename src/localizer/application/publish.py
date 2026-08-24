from __future__ import annotations

from functools import partial
from typing import Optional

from localizer.adapters.publishers.cloudflare_r2 import (
    Boto3R2Client,
    CloudflareR2Publisher,
)
from localizer.adapters.publishers.github_release import (
    GitHubReleasePublisher,
    UrllibGitHubReleaseClient,
)
from localizer.adapters.publishers.local import LocalPublisher
from localizer.adapters.publishers.alibaba_oss import (
    AlibabaOSSPublisher,
    OSS2STSClient,
    OSSSTSCredentialProvider,
)
from localizer.application.artifact import ReleaseBundle
from localizer.config.models import (
    GovernanceError,
    PublishSection,
    PublishTargetSection,
    SecuritySection,
)
from localizer.ports.publisher import Publisher, TargetResult


def publisher_from_config(
    target: PublishTargetSection,
    *,
    security: Optional[SecuritySection] = None,
) -> Publisher:
    """Construct a Publisher. Calling this for a remote target reads credentials, but does not connect.

    `security` 是显式凭据事件的闸门。生产路径会传入项目配置；只有配置声明
    `credential_rotation_required: true` 且轮换尚未完成时才拦截远端目标。
    """
    if target.type != "local" and security is not None:
        # 在**构造之前**拦下：构造 GitHubReleasePublisher 就已经把 token 从环境
        # 读出来了，闸门必须早于这一步。
        security.assert_remote_publishing_allowed(target.type)
    if target.type == "local":
        if target.destination is None:
            raise ValueError("local publisher requires destination")
        return LocalPublisher(
            target.destination, versioned_prefix=target.versioned_prefix
        )
    if target.type == "github_release":
        missing = [
            name
            for name, value in (
                ("repository", target.repository),
                ("token_env", target.token_env),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "github_release publisher missing: " + ", ".join(missing)
            )
        client = UrllibGitHubReleaseClient(token_env=target.token_env)
        return GitHubReleasePublisher(
            client, repository=target.repository, tag=target.tag
        )
    if target.type == "alibaba_oss":
        missing = [
            name
            for name, value in (
                ("endpoint", target.endpoint),
                ("bucket", target.bucket),
                ("sts_token_url", target.sts_token_url),
                ("sts_token_env", target.sts_token_env),
            )
            if not value
        ]
        if missing:
            raise ValueError("alibaba_oss publisher missing: " + ", ".join(missing))
        credentials = OSSSTSCredentialProvider(
            url=target.sts_token_url,
            token_env=target.sts_token_env,
            token_header=target.sts_token_header,
            timeout_seconds=target.timeout_seconds,
        )
        client = OSS2STSClient(endpoint=target.endpoint, credentials=credentials)
        return AlibabaOSSPublisher(
            client, bucket=target.bucket, prefix=target.prefix,
            versioned_prefix=target.versioned_prefix,
        )
    missing = [
        name
        for name, value in (
            ("bucket", target.bucket),
            ("access_key_env", target.access_key_env),
            ("secret_key_env", target.secret_key_env),
        )
        if not value
    ]
    if not target.account_id and not target.endpoint_url:
        missing.append("account_id or endpoint_url")
    if missing:
        raise ValueError("cloudflare_r2 publisher missing: " + ", ".join(missing))
    client = Boto3R2Client(
        account_id=target.account_id,
        access_key_env=target.access_key_env,
        secret_key_env=target.secret_key_env,
        endpoint_url=target.endpoint_url,
    )
    return CloudflareR2Publisher(
        client, bucket=target.bucket, prefix=target.prefix,
        versioned_prefix=target.versioned_prefix,
    )


# 这些异常类表示「远端暂时不可用」，重试有意义。程序错误（NameError/TypeError/
# AttributeError）必须归为 internal 且 retryable=False —— F24 的验收明确要求
# 「NameError 不得被伪装成 upload_failed」，否则一个拼写错误会被当成网络抖动无限重试。
_RETRYABLE_ERRORS = (TimeoutError, ConnectionError, OSError)
_INTERNAL_ERRORS = (NameError, TypeError, AttributeError, ImportError)

# 治理拒绝不是故障，更不是「暂时不可用」：重试一万次也不会变。
_GOVERNANCE_ERRORS = (GovernanceError,)


class PublishOrchestrator:
    """按配置逐个发布，任一目标失败不影响其余，也不触碰本地制品。

    [F24]：旧实现里 publisher_from_config 只被测试调用，生产路径完全不读
    config.publish.targets —— 配了三个目标也只有 publish-local 那一个会执行，
    退出码 0、无任何告警。GitHub 有档而 R2 缺档的原始故障形态原样保留。
    """

    def __init__(
        self, factory=None, *, security: Optional[SecuritySection] = None
    ) -> None:
        # 默认状态表示「没有声明凭据事件」；Provider 凭据与权限检查仍照常执行。
        self.factory = factory or partial(
            publisher_from_config, security=security or SecuritySection()
        )

    def publish(
        self, bundle: ReleaseBundle, publish: PublishSection
    ) -> "list[TargetResult]":
        # 先校验一次制品：mode==release、quality_gate_passed、SHA-256 与 Manifest 一致。
        # 任何目标都不该拿到没通过闸门的产物。
        bundle.verify()
        results = []
        for target in publish.targets:
            results.append(self._publish_one(bundle, target))
        return results

    def _publish_one(
        self, bundle: ReleaseBundle, target: PublishTargetSection
    ) -> TargetResult:
        label = target.type
        try:
            publisher = self.factory(target)
            receipt = publisher.publish(bundle)
        except _GOVERNANCE_ERRORS as exc:
            # 单目标隔离照旧：local 目标不受影响，仍然会被发布。
            return TargetResult(
                label, "failed", None, "governance", str(exc), False
            )
        except _INTERNAL_ERRORS as exc:
            # 程序错误单独归类，绝不标 retryable —— 重试一万次也还是这个错。
            return TargetResult(
                label, "failed", None, "internal", f"{type(exc).__name__}: {exc}", False
            )
        except _RETRYABLE_ERRORS as exc:
            return TargetResult(
                label, "failed", None, type(exc).__name__, str(exc), True
            )
        except Exception as exc:
            return TargetResult(
                label, "failed", None, type(exc).__name__, str(exc), False
            )
        return TargetResult(label, "succeeded", receipt)
