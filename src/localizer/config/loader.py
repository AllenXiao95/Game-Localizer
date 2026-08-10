from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

import yaml
from pydantic import ValidationError

from localizer.infrastructure.dotenv import load_dotenv_files

from .models import ProjectConfig


class ConfigLoadError(RuntimeError):
    pass


def load_project_config(path: Path) -> ProjectConfig:
    config_path = Path(path).resolve()
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigLoadError(f"cannot read project config {config_path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ConfigLoadError(f"project config {config_path} must contain a mapping")
    try:
        config = _validate_project_config(raw)
    except ValidationError as exc:
        raise ConfigLoadError(f"invalid project config {config_path}: {exc}") from exc
    config = _resolve_paths(config, config_path.parent)
    config = _resolve_environment(config, config_path)
    try:
        load_dotenv_files(
            config.environment.dotenv_files,
            override=config.environment.override_existing,
        )
    except (OSError, ValueError) as exc:
        raise ConfigLoadError(
            f"cannot load dotenv for project config {config_path}: {exc}"
        ) from exc
    return config


def _resolve_paths(config: ProjectConfig, base: Path) -> ProjectConfig:
    data: dict[str, Any] = (
        config.model_dump() if hasattr(config, "model_dump") else config.dict()
    )
    for key in ("workspace", "output"):
        data["paths"][key] = _resolve(base, Path(data["paths"][key]))
    if data["paths"].get("source") is not None:
        data["paths"]["source"] = _resolve(base, Path(data["paths"]["source"]))
    # 多目录项目（正式服/测试服等）的每个变体路径同样按配置文件位置解析。
    data["paths"]["sources"] = {
        name: _resolve(base, Path(value))
        for name, value in (data["paths"].get("sources") or {}).items()
    }
    data["cache"]["root"] = _resolve(base, Path(data["cache"]["root"]))
    data["environment"]["dotenv_files"] = [
        _resolve(base, Path(value))
        for value in data["environment"].get("dotenv_files", [])
    ]
    data["prompt"]["template"] = _resolve(base, Path(data["prompt"]["template"]))
    if data["prompt"].get("background") is not None:
        data["prompt"]["background"] = _resolve(base, Path(data["prompt"]["background"]))
    data["glossary"]["file"] = _resolve(base, Path(data["glossary"]["file"]))
    data["rules"]["file"] = _resolve(base, Path(data["rules"]["file"]))
    data["tm"]["database"] = _resolve(base, Path(data["tm"]["database"]))
    if data.get("quality_gate", {}).get("legacy_debt_baseline") is not None:
        data["quality_gate"]["legacy_debt_baseline"] = _resolve(
            base, Path(data["quality_gate"]["legacy_debt_baseline"])
        )
    for target in data["publish"]["targets"]:
        if target.get("destination") is not None:
            target["destination"] = _resolve(base, Path(target["destination"]))
    return _validate_project_config(data)


def _resolve_environment(config: ProjectConfig, config_path: Path) -> ProjectConfig:
    data: dict[str, Any] = (
        config.model_dump() if hasattr(config, "model_dump") else config.dict()
    )
    explicit = [Path(value).resolve() for value in data["environment"]["dotenv_files"]]
    discovered = []
    if config.environment.auto_discover:
        parents = []
        for parent in [config_path.parent, *config_path.parent.parents]:
            parents.append(parent)
            if (parent / ".git").exists() or (parent / "pyproject.toml").is_file():
                break
        for parent in reversed(parents):
            candidate = parent / ".env"
            if candidate.is_file():
                discovered.append(candidate.resolve())
    ordered = []
    seen = set()
    for path in (*discovered, *explicit):
        normalized = os.path.normcase(str(path))
        if normalized not in seen:
            seen.add(normalized)
            ordered.append(path)
    data["environment"]["dotenv_files"] = ordered
    return _validate_project_config(data)


def _validate_project_config(data: Mapping[str, Any]) -> ProjectConfig:
    if hasattr(ProjectConfig, "model_validate"):
        return ProjectConfig.model_validate(data)
    return ProjectConfig.parse_obj(data)


def _resolve(base: Path, value: Path) -> Path:
    return value.resolve() if value.is_absolute() else (base / value).resolve()
