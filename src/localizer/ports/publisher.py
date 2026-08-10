from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Tuple

from localizer.application.artifact import ReleaseBundle


@dataclass(frozen=True)
class PublishedObject:
    name: str
    locator: str
    sha256: str
    size: int
    skipped: bool = False


@dataclass(frozen=True)
class PublishReceipt:
    target: str
    objects: Tuple[PublishedObject, ...]


class Publisher(Protocol):
    def publish(self, bundle: ReleaseBundle) -> PublishReceipt:
        ...


@dataclass(frozen=True)
class TargetResult:
    """单个发布目标的结果。

    [F24]「镜像上传失败后保留本地制品」要求能表达「谁成功、谁失败、能否重试」。
    此前 PublishReceipt 只有 target 和 objects，连失败都表达不了，
    行为保留矩阵要求的 6 组断言在旧 API 下无法书写。
    """

    target: str
    status: str  # "succeeded" | "failed"
    receipt: Optional[PublishReceipt] = None
    error_class: Optional[str] = None
    error_message: str = ""
    retryable: bool = False

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "status": self.status,
            "retryable": self.retryable,
            "error_class": self.error_class,
            "error_message": self.error_message,
            "objects": [obj.__dict__ for obj in (self.receipt.objects if self.receipt else ())],
        }
