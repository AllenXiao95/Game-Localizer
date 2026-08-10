from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, Mapping, Optional, Sequence

from localizer.domain.translation_unit import TranslationUnit
from localizer.ports.provider import ProviderResponse, ProviderUsage


class ProviderError(RuntimeError):
    retryable = False


class TransientProviderError(ProviderError):
    retryable = True


class ReadTimeoutError(TransientProviderError):
    """请求已经发出去了，等响应超时。

    必须与「建连失败 / 429 / 5xx」分开：那几种是端点整体不可用或在限流，缩批只会
    在同一个端点上打出更多请求（实测持续 429 下 16 条批次打出 93 次请求）。
    而读超时说明**这一次请求本身太重** —— 2026-08-04 真机 preview 里 98 个失败
    有 97 个来自单独一个 97 条批次连续三次撞 120 秒读超时，那 97 条本身没问题，
    拆开重投就能过。对它缩批是对症的。
    """


class PermanentProviderError(ProviderError):
    retryable = False


Transport = Callable[[str, Mapping[str, str], bytes, float], Mapping[str, object]]


_RESERVED_CUSTOM_PARAMETERS = {
    "model",
    "messages",
    "max_tokens",
    "temperature",
    "stream",
    "n",
}
_SENSITIVE_PARAMETER_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "access_key",
    "accesskey",
    "access_token",
    "accesstoken",
    "api_token",
    "auth_token",
    "bearer_token",
    "token",
    "secret",
    "secret_key",
    "client_secret",
    "password",
}


def validate_custom_parameters(value: Mapping[str, object]) -> Dict[str, object]:
    """校验可直接合并进 OpenAI-compatible JSON 请求体的扩展参数。"""

    result = dict(value)
    collisions = sorted(_RESERVED_CUSTOM_PARAMETERS.intersection(result))
    if collisions:
        raise ValueError(
            "provider.custom_parameters cannot override framework-owned fields: "
            + ", ".join(collisions)
        )

    def visit(item: object, path: str = "custom_parameters") -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                if not isinstance(key, str):
                    raise ValueError(f"{path} keys must be strings")
                normalized = key.lower().replace("-", "_")
                if normalized in _SENSITIVE_PARAMETER_KEYS:
                    raise ValueError(
                        f"{path}.{key} looks like a credential; use an *_env field instead"
                    )
                visit(nested, f"{path}.{key}")
        elif isinstance(item, (list, tuple)):
            for index, nested in enumerate(item):
                visit(nested, f"{path}[{index}]")

    visit(result)
    try:
        # allow_nan=False 保证配置真的是可移植 JSON，而不是 Python 的 NaN/Infinity 扩展。
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("provider.custom_parameters must be valid JSON") from exc
    return result


@dataclass(frozen=True)
class OpenAICompatibleSettings:
    base_url: str
    api_key_env: str
    model: str
    temperature: float = 0.3
    timeout_seconds: float = 120
    max_output_tokens: int = 4096
    custom_parameters: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "custom_parameters",
            validate_custom_parameters(self.custom_parameters),
        )


class OpenAICompatibleProvider:
    """Minimal chat-completions client with an injectable HTTP transport."""

    def __init__(
        self,
        settings: OpenAICompatibleSettings,
        *,
        transport: Optional[Transport] = None,
    ) -> None:
        self.settings = settings
        self.transport = transport or self._urlopen_transport

    def translate(
        self, prompt: str, units: Sequence[TranslationUnit]
    ) -> ProviderResponse:
        api_key = os.environ.get(self.settings.api_key_env)
        if not api_key:
            raise PermanentProviderError(
                f"provider credential environment variable is unset: {self.settings.api_key_env}"
            )
        endpoint = self.settings.base_url.rstrip("/") + "/chat/completions"
        payload = dict(self.settings.custom_parameters)
        payload.update(
            {
                "model": self.settings.model,
                "temperature": self.settings.temperature,
                "max_tokens": self.settings.max_output_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
        )
        try:
            raw = self.transport(
                endpoint,
                {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                self.settings.timeout_seconds,
            )
        except TransientProviderError:
            raise
        except PermanentProviderError:
            raise
        except (TimeoutError, socket.timeout) as exc:
            raise ReadTimeoutError(str(exc)) from exc
        except ConnectionError as exc:
            # 建连失败：端点整体不可用，缩批毫无帮助。
            raise TransientProviderError(str(exc)) from exc
        return self._decode(raw)

    @staticmethod
    def _decode(raw: Mapping[str, object]) -> ProviderResponse:
        try:
            choices = raw["choices"]
            choice = choices[0]  # type: ignore[index]
            text = choice["message"]["content"]  # type: ignore[index]
            finish_reason = choice.get("finish_reason")  # type: ignore[union-attr]
            usage = raw.get("usage", {})
            if not isinstance(text, str) or not isinstance(usage, Mapping):
                raise TypeError
        except (KeyError, IndexError, TypeError) as exc:
            raise PermanentProviderError("invalid OpenAI-compatible response schema") from exc
        return ProviderResponse(
            text=text,
            finish_reason=str(finish_reason) if finish_reason is not None else None,
            usage=ProviderUsage(
                input_tokens=int(usage.get("prompt_tokens", 0)),
                output_tokens=int(usage.get("completion_tokens", 0)),
            ),
            provider_metadata={"id": raw.get("id"), "model": raw.get("model")},
        )

    @staticmethod
    def _urlopen_transport(
        url: str, headers: Mapping[str, str], body: bytes, timeout: float
    ) -> Mapping[str, object]:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            message = f"provider HTTP {exc.code}"
            if exc.code == 429 or 500 <= exc.code <= 599:
                raise TransientProviderError(message) from exc
            raise PermanentProviderError(message) from exc
        except socket.timeout as exc:
            raise ReadTimeoutError(str(exc)) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                raise ReadTimeoutError(str(exc.reason)) from exc
            raise TransientProviderError(str(exc)) from exc
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise PermanentProviderError("provider returned invalid JSON") from exc
        if not isinstance(decoded, Mapping):
            raise PermanentProviderError("provider response root must be an object")
        return decoded
