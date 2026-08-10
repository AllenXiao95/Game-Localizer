from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, PrivateAttr, root_validator, validator


class StrictModel(BaseModel):
    class Config:
        extra = "forbid"
        validate_assignment = True


# §16「配置文件只能保存环境变量名，不能保存 API Key 本身」。
#
# 原先三处 validator 各写一遍 `[A-Za-z_][A-Za-z0-9_]*`，那实际上只是「合法 C 标识符」
# 检查：它拦得住带连字符的 sk-xxx，却放行绝大多数纯 base62 的真密钥
# （AIzaSy… / hf_… / ghp_… / AKIA… 实测全部通过并写进 project.yaml 提交入库）。
#
# 收紧为环境变量的通行写法（全大写 + 下划线 + 数字），再加一层已知前缀 denylist
# 兜底——大写约定本身就能挡掉上面几种，denylist 是为了给出更准确的报错。
_ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")

# 常见凭据前缀。命中时报错要说清是「像密钥」而不是「命名不规范」。
_SECRET_PREFIXES = (
    "sk-", "sk_", "pk_", "rk_",           # OpenAI / Stripe 系
    "AIza",                                # Google / Gemini
    "hf_",                                 # HuggingFace
    "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_",  # GitHub
    "xox",                                 # Slack
    "AKIA", "ASIA",                        # AWS
    "pt_",                                 # ParaTranz
    "glpat-",                              # GitLab
)


def validate_env_var_reference(value: str, field: str) -> str:
    """确认取值是**环境变量名**而不是密钥本身。"""
    candidate = value or ""
    lowered = candidate.lower()
    if any(lowered.startswith(prefix.lower()) for prefix in _SECRET_PREFIXES):
        raise ValueError(
            f"{field} looks like a credential value, not an environment variable name; "
            f"put the secret in the environment and reference it by name"
        )
    if not _ENV_NAME_RE.fullmatch(candidate):
        raise ValueError(
            f"{field} must be an environment variable NAME "
            f"(upper snake case, 3-64 chars, e.g. LOCALIZER_API_KEY), "
            f"never the credential value itself"
        )
    return candidate


class ProjectSection(StrictModel):
    id: str
    name: str
    game_version: str

    @validator("game_version")
    def game_version_must_not_repeat_release_prefix(cls, value: str) -> str:
        candidate = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", candidate):
            raise ValueError(
                "project.game_version must be 1-64 path-safe characters"
            )
        if re.match(r"^[vV]\d", candidate):
            raise ValueError(
                "project.game_version must not start with 'v'; release naming adds it"
            )
        return candidate


class PathsSection(StrictModel):
    """资源目录。

    同一个游戏常有多个并行的资源目录 —— 正式服与测试服（WoT 的
    `E:/Tanki` 与 `E:/Tanki_PT`）、不同分发渠道、不同客户端版本。它们**必须共享**
    翻译记忆库与术语表：一条译文在正式服定稿了，测试服不该再花钱翻一遍。

    共享是**结构性**的，不靠配置开关：`stable_identity` 由
    project_id + adapter_id + relative_path + logical_key 构成，不含变体，
    因此同一个 key 在两个目录里天然是同一条 TM 记录。而 `lookup` 会比对
    `source_fingerprint`，所以测试服改了源文的条目自然不命中、会重新翻译 ——
    共享和隔离都不需要额外开关。

    - 单目录项目继续只写 `source`，布局与行为完全不变；
    - 多目录项目写 `sources`，工作区与输出会按变体分开，避免 run 之间互相覆盖。
    """

    source: Optional[Path] = None
    sources: Dict[str, Path] = Field(default_factory=dict)
    default_variant: Optional[str] = None
    workspace: Path
    output: Path

    @validator("sources")
    def variant_names_must_be_path_safe(cls, value: Dict[str, Path]) -> Dict[str, Path]:
        # 变体名会进工作区与输出路径。
        for name in value:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}", name or ""):
                raise ValueError(
                    f"invalid source variant name {name!r}: 1-32 chars of letters, "
                    f"digits, dot, dash or underscore, starting with a letter or digit"
                )
        return value

    @root_validator(skip_on_failure=True)
    def require_a_source(cls, values: dict) -> dict:
        single = values.get("source")
        multiple = values.get("sources") or {}
        if single is None and not multiple:
            raise ValueError("paths requires either `source` or `sources`")
        default = values.get("default_variant")
        if default is not None and default not in multiple:
            raise ValueError(
                f"paths.default_variant {default!r} is not one of "
                f"{sorted(multiple) or '(no sources declared)'}"
            )
        return values

    @property
    def variants(self) -> Dict[str, Path]:
        """变体名 -> 目录。单目录项目返回空字典（没有变体这个概念）。"""
        return dict(self.sources)

    def resolve_variant(self, variant: Optional[str] = None) -> str:
        """选出要跑的变体名；单目录项目返回空串。"""
        if not self.sources:
            if variant:
                raise ValueError(
                    f"project declares a single `source`; unknown variant {variant!r}"
                )
            return ""
        if variant:
            if variant not in self.sources:
                raise ValueError(
                    f"unknown source variant {variant!r}; available: "
                    f"{sorted(self.sources)}"
                )
            return variant
        if self.default_variant:
            return self.default_variant
        if len(self.sources) == 1:
            return next(iter(self.sources))
        raise ValueError(
            f"project declares {len(self.sources)} source variants "
            f"({sorted(self.sources)}); pass --variant or set paths.default_variant"
        )


class CacheSection(StrictModel):
    """Cross-project caches that are safe to regenerate and must not enter Git."""

    root: Path = Path("../../var/cache")
    scope: Literal["shared", "project"] = "shared"

    @property
    def tokenizers(self) -> Path:
        # Keep one canonical layout instead of allowing every provider to invent
        # another tokenizer directory.  Model/revision separation is managed by
        # the tokenizer implementation below this root (for example Hugging Face).
        return self.root / "tokenizers"


class EnvironmentSection(StrictModel):
    """可选 dotenv 来源；配置与 API 只保存文件路径，不保存变量值。"""

    dotenv_files: List[Path] = Field(default_factory=list)
    auto_discover: bool = False
    override_existing: bool = False


class LanguagesSection(StrictModel):
    source: str
    target: str


class ResourceAdapterSection(StrictModel):
    type: str
    include: List[str] = Field(default_factory=lambda: ["**/*"])
    exclude: List[str] = Field(default_factory=list)
    # Adapter 专有选项由各 Adapter 自己声明的严格 Schema 校验；内核不解释语义。
    options: Dict[str, Any] = Field(default_factory=dict)

    @validator("type")
    def type_must_be_registered(cls, value: str) -> str:
        # 在 validate-config 阶段就拦住拼错的 type，而不是等扫描时才炸。
        from localizer.adapters.resources.registry import available_adapters

        known = available_adapters()
        if value not in known:
            raise ValueError(
                f"unknown adapter type {value!r}; available: {list(known)}"
            )
        return value

    @root_validator(skip_on_failure=True)
    def options_must_match_adapter_schema(cls, values: dict) -> dict:
        from localizer.adapters.resources.registry import validate_adapter_options

        adapter_type = values.get("type")
        if adapter_type:
            values["options"] = validate_adapter_options(
                adapter_type, values.get("options") or {}
            )
        return values


class ResourcesSection(StrictModel):
    adapters: List[ResourceAdapterSection]

    @validator("adapters")
    def require_adapter(cls, value: List[ResourceAdapterSection]) -> List[ResourceAdapterSection]:
        if not value:
            raise ValueError("at least one resource adapter is required")
        return value


class PromptSection(StrictModel):
    template: Path
    background: Optional[Path] = None


class GlossarySection(StrictModel):
    file: Path
    auto_discovery: Literal["disabled", "candidate_only"] = "candidate_only"


class RulesSection(StrictModel):
    file: Path


class TokenizerSection(StrictModel):
    """Optional local tokenizer identity; independent from the provider API model."""

    type: Literal["huggingface"] = "huggingface"
    model: str
    revision: Optional[str] = None
    local_files_only: bool = False

    @validator("model")
    def tokenizer_model_must_not_be_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("tokenizer model must not be empty")
        return value


class ProviderSection(StrictModel):
    type: Literal["openai_compatible"] = "openai_compatible"
    base_url: str
    api_key_env: str
    model: str
    temperature: float = 0.3
    timeout_seconds: int = 120
    concurrency: int = 4
    rpm: Optional[int] = None
    tpm: Optional[int] = None
    context_window: int = 32000
    max_output_tokens: int = 4096
    tokenizer: Optional[TokenizerSection] = None
    custom_parameters: Dict[str, Any] = Field(default_factory=dict)

    @validator("api_key_env")
    def key_must_be_reference(cls, value: str) -> str:
        return validate_env_var_reference(value, "api_key_env")

    @validator("concurrency")
    def concurrency_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("concurrency must be positive")
        return value

    @validator("custom_parameters")
    def custom_parameters_must_be_safe_json(
        cls, value: Dict[str, Any]
    ) -> Dict[str, Any]:
        from localizer.adapters.providers.openai_compatible import (
            validate_custom_parameters,
        )

        return validate_custom_parameters(value)

    @root_validator(skip_on_failure=True)
    def token_limits_must_be_consistent(cls, values: dict) -> dict:
        context_window = int(values.get("context_window") or 0)
        max_output_tokens = int(values.get("max_output_tokens") or 0)
        if context_window <= 0:
            raise ValueError("provider.context_window must be positive")
        if max_output_tokens <= 0:
            raise ValueError("provider.max_output_tokens must be positive")
        if max_output_tokens >= context_window:
            raise ValueError(
                "provider.max_output_tokens must be smaller than context_window"
            )
        return values


class TranslationMemorySection(StrictModel):
    database: Path
    global_exact_match: Literal[
        "disabled", "reviewed_only", "reviewed_or_legacy_converged"
    ] = "reviewed_only"
    commit_policy: Literal["quality_gate"] = "quality_gate"


class ParaTranzSyncSection(StrictModel):
    """ParaTranz 同步策略。

    `dry_run_by_default` 默认 True 是 §10 的硬性要求：写向社区平台的操作，
    默认必须只产出变更预览。`delete_policy` 目前只允许 `report_only` ——
    没有 API Client 的情况下把删除写成可执行选项，会给人「删除已实现」的错觉。
    """

    dry_run_by_default: bool = True
    delete_policy: Literal["report_only"] = "report_only"


class WorkflowSection(StrictModel):
    mode: Literal["local", "paratranz"] = "local"
    project_id: Optional[int] = None
    token_env: Optional[str] = None
    # §10 的配置示例里就有这两项，但 Schema 一直没有 —— 照抄官方文档的配置
    # 连 `validate-config` 都过不了（extra_forbidden，EXIT=2）。
    # 注意：接受这两个字段**不等于** M5 已实现；API Client 仍然不存在，
    # 它们当前只被校验和透传，用于把文档与 Schema 钉在一起。
    minimum_release_stage: Optional[int] = None
    sync: Optional[ParaTranzSyncSection] = None

    @validator("minimum_release_stage")
    def stage_must_be_a_known_paratranz_stage(
        cls, value: Optional[int]
    ) -> Optional[int]:
        # ParaTranz 官方 stage：0 未译 / 1 已译 / 2 存疑 / 3 已检查 /
        # 5 已审核 / 9 已锁定。-1（隐藏）不能当作发布门槛。
        if value is not None and value not in {0, 1, 2, 3, 5, 9}:
            raise ValueError(
                "minimum_release_stage must be one of the ParaTranz stages "
                "0/1/2/3/5/9"
            )
        return value

    @validator("project_id", always=True)
    def paratranz_requires_project(cls, value: Optional[int], values: dict) -> Optional[int]:
        if values.get("mode") == "paratranz" and value is None:
            raise ValueError("project_id is required for paratranz workflow")
        return value

    @validator("token_env", always=True)
    def token_must_be_environment_reference(
        cls, value: Optional[str], values: dict
    ) -> Optional[str]:
        if values.get("mode") == "paratranz" and value is None:
            raise ValueError("token_env is required for paratranz workflow")
        if value is not None:
            validate_env_var_reference(value, "token_env")
        return value


class QualityGateSection(StrictModel):
    """release 闸门。

    本次运行自己产出的译文上的 error 永远零容忍。搬运过来的存量译文
    （TM 命中、ParaTranz 回流、资源自带）上的 error 属于存量债：只有登记在
    `legacy_debt_baseline` 里的才放行，新增的一律阻断 —— 债只能减不能增。

    不配置基线时行为与以前完全一致（全量零容忍），不会悄悄放松。
    """

    legacy_debt_baseline: Optional[Path] = None


class CompatibilityMetadataSection(StrictModel):
    enabled: bool = False
    format: Literal["legacy_v6"] = "legacy_v6"
    filename: str = "metadata.json"
    env: Optional[str] = None

    @validator("filename")
    def filename_must_be_a_safe_basename(cls, value: str) -> str:
        if Path(value).name != value or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value or ""
        ):
            raise ValueError("compatibility metadata filename must be a safe basename")
        return value

    @root_validator(skip_on_failure=True)
    def enabled_metadata_requires_environment(cls, values: dict) -> dict:
        if values.get("enabled") and not values.get("env"):
            raise ValueError("enabled compatibility metadata requires env")
        return values


class BuildSection(StrictModel):
    format: Literal["zip"] = "zip"
    release_channel: str = "local-release"
    # 为空时保留旧的 project_id-run_id.zip 命名；项目显式配置后才启用版本派生。
    variant: Optional[str] = None
    artifact_prefix: str = "i18n"
    compression: Literal["deflate", "lzma", "stored"] = "deflate"
    encryption: Literal["none", "aes256"] = "none"
    password_env: Optional[str] = None
    archive_root: Optional[str] = None
    compatibility_metadata: CompatibilityMetadataSection = Field(
        default_factory=CompatibilityMetadataSection
    )

    @validator("variant", "artifact_prefix")
    def release_component_must_be_path_safe(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value or ""):
            raise ValueError(
                "release naming components must be 1-64 path-safe characters"
            )
        return value

    @validator("password_env")
    def password_must_be_environment_reference(
        cls, value: Optional[str]
    ) -> Optional[str]:
        if value is not None:
            validate_env_var_reference(value, "build.password_env")
        return value

    @validator("archive_root")
    def archive_root_must_be_safe_relative_path(
        cls, value: Optional[str]
    ) -> Optional[str]:
        if value is None:
            return value
        candidate = value.replace("\\", "/").strip("/")
        parts = candidate.split("/") if candidate else []
        if not parts or any(
            part in {"", ".", ".."}
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", part)
            for part in parts
        ):
            raise ValueError("build.archive_root must be a safe relative archive path")
        return "/".join(parts)

    @root_validator(skip_on_failure=True)
    def encrypted_archive_requires_password_reference(cls, values: dict) -> dict:
        if values.get("encryption") == "aes256" and not values.get("password_env"):
            raise ValueError("AES-256 archive requires build.password_env")
        if values.get("encryption") == "none" and values.get("password_env"):
            raise ValueError("build.password_env is only valid with AES-256 encryption")
        return values


class PublishTargetSection(StrictModel):
    type: Literal["local", "github_release", "cloudflare_r2", "alibaba_oss"]
    destination: Optional[Path] = None
    repository: Optional[str] = None
    tag: Optional[str] = None
    token_env: Optional[str] = None
    account_id: Optional[str] = None
    endpoint_url: Optional[str] = None
    bucket: Optional[str] = None
    prefix: str = ""
    # 默认 false 保持旧配置中 prefix 的精确语义；新项目可显式开启版本段追加。
    versioned_prefix: bool = False
    access_key_env: Optional[str] = None
    secret_key_env: Optional[str] = None
    endpoint: Optional[str] = None
    sts_token_url: Optional[str] = None
    sts_token_env: Optional[str] = None
    sts_token_header: str = "token"
    timeout_seconds: int = 120

    @validator("token_env", "access_key_env", "secret_key_env", "sts_token_env")
    def credential_must_be_environment_reference(
        cls, value: Optional[str]
    ) -> Optional[str]:
        if value is not None:
            validate_env_var_reference(value, "publisher credential")
        return value

    @validator("sts_token_header")
    def sts_header_must_be_safe(cls, value: str) -> str:
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", value):
            raise ValueError("sts_token_header must be a valid HTTP header name")
        return value

    @validator("timeout_seconds")
    def publish_timeout_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("publisher timeout_seconds must be positive")
        return value

    @root_validator(skip_on_failure=True)
    def require_fields_for_target(cls, values: dict) -> dict:
        target_type = values.get("type")
        required = {
            "local": ("destination",),
            # tag 为空时由正式制品中的 release.slug（例如 ru-v1.44.0.0）派生。
            "github_release": ("repository", "token_env"),
            "alibaba_oss": (
                "endpoint",
                "bucket",
                "sts_token_url",
                "sts_token_env",
            ),
        }.get(target_type, ())
        missing = [name for name in required if not values.get(name)]
        if target_type == "cloudflare_r2":
            missing.extend(
                name
                for name in ("bucket", "access_key_env", "secret_key_env")
                if not values.get(name)
            )
            if not values.get("account_id") and not values.get("endpoint_url"):
                missing.append("account_id or endpoint_url")
        if missing:
            raise ValueError(
                f"{target_type} publisher missing: " + ", ".join(missing)
            )
        return values


class ReviewSection(StrictModel):
    """QA 缺陷的定点修复（framework-design §16.4）。

    面板对 QA 报告已识别的问题提供**单人**定稿编辑。权威记录是这里指定的
    append-only 决策日志；TM 只是它的可重放投影。不配置时用项目目录下的默认值。
    """

    # append-only 决策日志的基准路径。实际按月分片为 decisions-YYYYMM.jsonl。
    decisions_file: Optional[Path] = None
    # 每次落表的操作者标识。不是密码学身份 —— 面板无认证、只绑回环，
    # 它能追溯到的是「哪个会话、哪个 OS 账户、哪台机器」。
    reviewer: Optional[str] = None


class GovernanceError(RuntimeError):
    """治理闸门拒绝 —— 不是网络故障，重试无意义。"""


class SecuritySection(StrictModel):
    """凭据治理未完成时禁用一切远端发布目标。

    默认 fail-closed：不填 `credential_rotation_completed_at` 就发不了远端。
    `local` 目标不受影响，因为本地打包不涉及远端凭据。
    """

    # 六个控制台的凭据轮换全部完成的日期（ISO-8601）。填这一项等于**签字**：
    # 有人确认旧凭据已失效、新凭据只存在于环境变量里。
    credential_rotation_completed_at: Optional[str] = None
    # 轮换记录的存放位置（工单号、审计文档路径、控制台截图目录皆可）。
    # 只填日期不留记录，事后无法核对到底轮换了哪几套。
    rotation_record: Optional[str] = None

    @validator("credential_rotation_completed_at")
    def must_be_iso_date(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        try:
            datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                "credential_rotation_completed_at must be an ISO-8601 date/datetime, "
                f"got {value!r}"
            ) from exc
        return value

    @property
    def remote_publishing_allowed(self) -> bool:
        return bool(self.credential_rotation_completed_at and self.rotation_record)

    def assert_remote_publishing_allowed(self, target_type: str) -> None:
        if self.remote_publishing_allowed:
            return
        missing = [
            name
            for name, value in (
                ("credential_rotation_completed_at", self.credential_rotation_completed_at),
                ("rotation_record", self.rotation_record),
            )
            if not value
        ]
        raise GovernanceError(
            f"remote publish target {target_type!r} is disabled: M0 凭据治理未完成"
            f"（缺 security.{' 与 security.'.join(missing)}）。"
            f"完成六套生产凭据轮换并留下记录后再填这两项；"
            f"只想本地打包请把 publish.targets 收敛为 type: local。"
        )


class PublishSection(StrictModel):
    targets: List[PublishTargetSection] = Field(default_factory=list)


class ProjectConfig(StrictModel):
    # 运行期投影信息，不属于 project.yaml Schema。PrivateAttr 在 Pydantic 1/2
    # 都不会进入 dict/model_dump；否则 Pydantic 1 会把 object.__setattr__ 注入的
    # `_active_variant` 导出成普通字段，WebUI 再 parse_obj 时因 extra=forbid 失败。
    _active_variant: str = PrivateAttr(default="")

    schema_version: Literal[1]
    project: ProjectSection
    paths: PathsSection
    cache: CacheSection = Field(default_factory=CacheSection)
    environment: EnvironmentSection = Field(default_factory=EnvironmentSection)
    languages: LanguagesSection
    resources: ResourcesSection
    prompt: PromptSection
    glossary: GlossarySection
    rules: RulesSection
    provider: ProviderSection
    tm: TranslationMemorySection
    workflow: WorkflowSection = Field(default_factory=WorkflowSection)
    build: BuildSection = Field(default_factory=BuildSection)
    quality_gate: QualityGateSection = Field(default_factory=QualityGateSection)
    security: SecuritySection = Field(default_factory=SecuritySection)
    review: ReviewSection = Field(default_factory=ReviewSection)
    publish: PublishSection = Field(default_factory=PublishSection)

    def for_game_version(self, game_version: str) -> "ProjectConfig":
        """Return a task-local projection without mutating project.yaml."""
        data = self.model_dump() if hasattr(self, "model_dump") else self.dict()
        data["project"]["game_version"] = game_version
        if hasattr(ProjectConfig, "model_validate"):
            return ProjectConfig.model_validate(data)
        return ProjectConfig.parse_obj(data)

    def for_variant(self, variant: Optional[str] = None) -> "ProjectConfig":
        """把多目录项目投影成一个单目录配置。

        下游全部代码继续只读 `paths.source`，不需要知道变体的存在 —— 这是刻意的：
        变体只是「同一个项目的另一个资源目录」，不是另一个项目。

        **TM 与术语表路径原样不动**，因此正式服与测试服天然共享：
        `stable_identity` 不含变体，同一个 key 在两个目录里就是同一条 TM 记录；
        而 lookup 比对 `source_fingerprint`，测试服改过的源文自然不命中、会重译。
        共享与隔离都不需要额外开关。

        工作区与输出按变体分开，避免两个变体的 run 互相覆盖。
        单目录项目（只写 `source`）布局完全不变。
        """
        name = self.paths.resolve_variant(variant)
        if not name:
            return self
        data = self.model_dump() if hasattr(self, "model_dump") else self.dict()
        data["paths"]["source"] = self.paths.sources[name]
        data["paths"]["workspace"] = self.paths.workspace / name
        data["paths"]["output"] = self.paths.output / name
        projected = (
            ProjectConfig.model_validate(data)
            if hasattr(ProjectConfig, "model_validate")
            else ProjectConfig.parse_obj(data)
        )
        object.__setattr__(projected, "_active_variant", name)
        return projected

    @property
    def active_variant(self) -> str:
        """当前配置对应的变体名；单目录项目为空串。"""
        return getattr(self, "_active_variant", "")
