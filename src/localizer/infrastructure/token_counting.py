from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional, Union

from localizer.application.token_budget import TokenCounter, conservative_token_count
from localizer.config.models import TokenizerSection


class HuggingFaceTokenCounter:
    """延迟加载、可跨 worker 共享的 Hugging Face Token 计数器。"""

    def __init__(self, config: TokenizerSection, cache_dir: Path) -> None:
        self.config = config
        self.cache_dir = Path(cache_dir)
        self._tokenizer = None
        self._load_error: Optional[BaseException] = None
        self._lock = threading.RLock()

    @property
    def resolved_source(self) -> Union[str, Path]:
        return resolve_tokenizer_source(self.config, self.cache_dir)

    def warm_up(self) -> None:
        """Resolve and load the tokenizer once before translation workers start."""

        self._load()

    def _load(self):
        with self._lock:
            if self._tokenizer is not None:
                return self._tokenizer
            if self._load_error is not None:
                raise RuntimeError(str(self._load_error)) from self._load_error
            try:
                from transformers import AutoTokenizer
            except ImportError as exc:
                raise RuntimeError(
                    "provider.tokenizer is configured but transformers is not installed; "
                    "install game-localizer[tokenizer-huggingface] or remove the tokenizer block"
                ) from exc
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            source: Union[str, Path] = self.config.model
            try:
                source = self.resolved_source
                if isinstance(source, Path):
                    # A resolved snapshot is self-contained. Loading the directory
                    # directly avoids Hub endpoint, refs and cache-format differences.
                    self._tokenizer = AutoTokenizer.from_pretrained(
                        str(source),
                        local_files_only=True,
                    )
                else:
                    self._tokenizer = AutoTokenizer.from_pretrained(
                        source,
                        revision=self.config.revision,
                        cache_dir=str(self.cache_dir),
                        local_files_only=self.config.local_files_only,
                    )
            except Exception as exc:
                message = (
                    "failed to initialize configured tokenizer "
                    f"model={self.config.model!r}, revision={self.config.revision!r}, "
                    f"cache={str(self.cache_dir)!r}, resolved_source={str(source)!r}: {exc}"
                )
                self._load_error = RuntimeError(message)
                raise self._load_error from exc
            return self._tokenizer

    def __call__(self, text: str) -> int:
        # 自定义 tokenizer 不一定声明线程安全；锁住 encode，保证多 worker 确定性。
        with self._lock:
            tokenizer = self._load()
            return len(tokenizer.encode(text, add_special_tokens=False))


def build_token_counter(
    config: Optional[TokenizerSection], cache_dir: Path
) -> TokenCounter:
    if config is None:
        return conservative_token_count
    if config.type == "huggingface":
        return HuggingFaceTokenCounter(config, cache_dir)
    raise ValueError(f"unsupported tokenizer type: {config.type}")


def warm_up_token_counter(counter: TokenCounter) -> None:
    warm_up = getattr(counter, "warm_up", None)
    if callable(warm_up):
        warm_up()


def resolve_tokenizer_source(
    config: TokenizerSection, cache_dir: Path
) -> Union[str, Path]:
    """Prefer an exact usable local snapshot, otherwise return the Hub model id.

    Hugging Face cache roots can contain several revisions. Merely finding the
    repository directory is insufficient: the configured revision must resolve
    to a snapshot containing tokenizer data. Once resolved, the snapshot path is
    passed directly to Transformers so no endpoint or Hub cache lookup is needed.
    """

    configured_path = Path(config.model).expanduser()
    if configured_path.is_dir():
        return configured_path.resolve(strict=True)

    snapshot = _cached_snapshot(config, Path(cache_dir))
    if snapshot is not None and _has_tokenizer_data(snapshot):
        return snapshot
    if config.local_files_only:
        detail = str(snapshot) if snapshot is not None else "not found"
        raise RuntimeError(
            "configured local tokenizer snapshot is unavailable or incomplete: "
            f"model={config.model!r}, revision={config.revision!r}, "
            f"cache={str(Path(cache_dir))!r}, snapshot={detail!r}; "
            "pin a cached revision containing tokenizer data or configure a local snapshot path"
        )
    return config.model


def _cached_snapshot(config: TokenizerSection, cache_dir: Path) -> Optional[Path]:
    parts = [part for part in config.model.replace("\\", "/").split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        return None
    repository = Path(cache_dir) / ("models--" + "--".join(parts))
    revision = config.revision or "main"
    snapshot = repository / "snapshots" / revision
    if not snapshot.is_dir():
        reference = repository / "refs" / Path(*revision.split("/"))
        try:
            commit = reference.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return None
        if not commit or any(char not in "0123456789abcdefABCDEF" for char in commit):
            return None
        snapshot = repository / "snapshots" / commit
    if not snapshot.is_dir():
        return None
    try:
        resolved = snapshot.resolve(strict=True)
        resolved.relative_to(repository.resolve(strict=True))
    except (OSError, ValueError):
        return None
    return resolved


def _has_tokenizer_data(snapshot: Path) -> bool:
    tokenizer_files = (
        "tokenizer.json",
        "tokenizer.model",
        "spiece.model",
        "sentencepiece.bpe.model",
        "vocab.json",
        "vocab.txt",
    )
    return (snapshot / "tokenizer_config.json").is_file() and any(
        (snapshot / name).is_file() for name in tokenizer_files
    )
