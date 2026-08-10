"""Paradox 本地化 YML Adapter（HOI4 / Stellaris / CK3 系）。

M7 的第二种资源格式。选它是因为它能压测到 Gettext 压不到的地方：

- **key 与源文彻底分离**。Gettext 把 msgid 当 logical_key，源文一改身份就变；
  Paradox 的 key 是独立的（`DEMO_focus:0 "Rearmament"`），正好验证
  `stable_identity` 不含 source_text 这条设计是否真的成立。
- **名叫 .yml 但不是合法 YAML**。`KEY:0 "值"` 不是 YAML 映射，值里还能有 `:` 和 `#`。
  probe 不能靠后缀判定，必须读首行；解析必须自己做词法而不是丢给 yaml.safe_load。
- **目录名与文件名都编码了语言**：`english/x_l_english.yml` →
  `simp_chinese/x_l_simp_chinese.yml`，首行 header 也要改写。这正是
  `plan_destination` 存在的理由 —— 内核原先把输出路径写死为「源相对路径」。
- **五种占位符语法**：`$VAR$`、`$VALUE|*1$`、`£icon£`、`§Y…§!`、`[Root.GetName]`，
  generic 预设对它们全部零匹配。
- **无复数、无 msgctxt**：正面证伪「内核依赖 Gettext 概念模型」。

不解包任何游戏资源；测试用的样本是手写的仿真语料（tests/fixtures/paradox/）。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from localizer.adapters.resources.registry import register_adapter
from localizer.adapters.resources.options import ParadoxYmlOptions, normalize_options
from localizer.domain.translation_unit import TranslationUnit
from localizer.infrastructure.atomic_io import AtomicIO
from localizer.ports.resource import (
    RenderResult,
    ResourceDescriptor,
    ValidationResult,
)
from localizer.rules.placeholder import register_placeholder_syntax

# 首行形如 `l_english:`，可带 BOM。
HEADER_RE = re.compile(r"^﻿?\s*l_([a-z_]+)\s*:\s*$")

# `␣KEY:0 "值"` 或 `␣KEY: "值"`；key 与值之间可以是空格或 TAB。
# 值用贪婪匹配到最后一个未转义引号 —— 值内允许出现 `:`、`#`、`\"`。
ENTRY_RE = re.compile(
    r"^(?P<indent>\s*)"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_.\-]*)"
    r"\s*:\s*(?P<version>\d*)"
    r"[ \t]+"
    r"(?P<value>\"(?:[^\"\\]|\\.)*\")"
    r"(?P<trailer>.*)$"
)

COMMENT_RE = re.compile(r"^\s*#")

# 语言目录名 ↔ locale。只列内建映射，其余走 options.locale_folders 覆盖。
DEFAULT_LOCALE_FOLDERS = {
    "en": "english",
    "en-US": "english",
    "zh-Hans": "simp_chinese",
    "zh-CN": "simp_chinese",
    "ru": "russian",
    "ru-RU": "russian",
    "fr": "french",
    "de": "german",
    "ja": "japanese",
    "ko": "korean",
}

PARADOX_PLACEHOLDERS = (
    r"\$[^$\s]*\$",           # $COUNTRY$ / $VALUE|*0$ / $other_key$
    r"£[^£\s]*£",             # £political_power£
    r"§.",                     # §Y … §!（颜色码，第二个字符是颜色标识）
    r"\[[A-Za-z][^\]\s]*\]",  # [Root.GetNameDef]
    r"\\n|\\t",               # 字面转义序列，不是真实换行
)

register_placeholder_syntax("paradox_yml", PARADOX_PLACEHOLDERS)


def _unquote(raw: str) -> str:
    """去掉外层引号并还原 `\\"` 转义。其余反斜杠序列原样保留（游戏自己解释）。"""
    inner = raw[1:-1]
    return inner.replace('\\"', '"')


def _quote(value: str) -> str:
    return '"' + value.replace('"', '\\"') + '"'


@register_adapter
class ParadoxYmlAdapter:
    adapter_id = "paradox_yml"
    options_model = ParadoxYmlOptions

    def __init__(
        self,
        *,
        project_id: str,
        source_root: Path,
        source_locale: str,
        target_locale: str,
        options: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.project_id = project_id
        self.source_root = Path(source_root).resolve()
        self.source_locale = source_locale
        self.target_locale = target_locale
        self.options = normalize_options(self.options_model, options)
        folders = dict(DEFAULT_LOCALE_FOLDERS)
        folders.update(self.options.get("locale_folders", {}))
        self.locale_folders = folders

    # ------------------------------------------------------------------ probe

    def probe(self, path: Path) -> float:
        """靠首行判定，不靠后缀。

        `.yml` 后缀被无数格式共用；反过来 Paradox 也有项目用 `.txt`。
        真正的判据是首行的 `l_<language>:` header。
        """
        path = Path(path)
        if not path.is_file():
            return 0.0
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                first = handle.readline()
        except (OSError, UnicodeDecodeError):
            return 0.0
        match = HEADER_RE.match(first.rstrip("\r\n"))
        if match is None:
            return 0.0
        # 语言不匹配的文件（french/…）不该被当成源文处理。
        expected = self.locale_folders.get(self.source_locale)
        if expected is not None and match.group(1) != expected:
            return 0.0
        return 0.95

    # ------------------------------------------------------------------- scan

    def scan(self, path: Path) -> ResourceDescriptor:
        path = Path(path).resolve()
        try:
            relative = path.relative_to(self.source_root).as_posix()
        except ValueError:
            relative = path.name
        return ResourceDescriptor(
            adapter_id=self.adapter_id,
            path=path,
            relative_path=relative,
            size=path.stat().st_size if path.is_file() else 0,
            confidence=self.probe(path),
        )

    # ---------------------------------------------------------------- extract

    def _parse(self, path: Path) -> Tuple[str, List[dict]]:
        """返回 (header 语言, 条目列表)。畸形行记进条目的 error 字段。"""
        text = Path(path).read_text(encoding="utf-8-sig")
        lines = text.splitlines()
        language = ""
        entries: List[dict] = []
        for number, line in enumerate(lines):
            if number == 0:
                header = HEADER_RE.match(line)
                if header:
                    language = header.group(1)
                    continue
            if not line.strip() or COMMENT_RE.match(line):
                continue
            match = ENTRY_RE.match(line)
            if match is None:
                entries.append({"line": number + 1, "raw": line, "error": "unparsable"})
                continue
            entries.append(
                {
                    "line": number + 1,
                    "key": match.group("key"),
                    "version": match.group("version"),
                    "value": _unquote(match.group("value")),
                    "indent": match.group("indent"),
                    "trailer": match.group("trailer"),
                }
            )
        return language, entries

    def extract(self, path: Path) -> Sequence[TranslationUnit]:
        path = Path(path).resolve()
        relative = self.scan(path).relative_path
        _language, entries = self._parse(path)
        sidecar = self._sidecar_translations(path)
        units = []
        seen = set()
        for entry in entries:
            if "error" in entry:
                continue
            key = entry["key"]
            if key in seen:
                # logical_key 必须每文件唯一：stable_identity 由它构成，
                # 重复会让下游的 placeholder_maps 之类的按身份索引的结构互相覆盖。
                raise ValueError(
                    f"duplicate key {key!r} in {relative} (line {entry['line']})"
                )
            seen.add(key)
            units.append(
                TranslationUnit(
                    project_id=self.project_id,
                    adapter_id=self.adapter_id,
                    relative_path=relative,
                    logical_key=key,
                    source_text=entry["value"],
                    source_locale=self.source_locale,
                    target_locale=self.target_locale,
                    translation=sidecar.get(key),
                    metadata={
                        "paradox_version": entry["version"],
                        "paradox_indent": entry["indent"],
                        "paradox_trailer": entry["trailer"],
                    },
                )
            )
        return tuple(units)

    def _sidecar_translations(self, source: Path) -> dict:
        """若目标语言目录下已有同名文件，把已有译文回填进 unit.translation。"""
        target = self._target_path_for(source)
        if target is None or not target.is_file() or target == source:
            return {}
        _language, entries = self._parse(target)
        return {
            entry["key"]: entry["value"]
            for entry in entries
            if "error" not in entry and entry["value"].strip()
        }

    # --------------------------------------------------------------- destination

    def _folder_names(self) -> Tuple[Optional[str], Optional[str]]:
        return (
            self.locale_folders.get(self.source_locale),
            self.locale_folders.get(self.target_locale),
        )

    def _rewrite_relative(self, relative: str) -> str:
        """english/foo_l_english.yml -> simp_chinese/foo_l_simp_chinese.yml"""
        source_folder, target_folder = self._folder_names()
        if not source_folder or not target_folder:
            return relative
        parts = relative.split("/")
        parts = [target_folder if part == source_folder else part for part in parts]
        name = parts[-1]
        parts[-1] = name.replace(f"l_{source_folder}", f"l_{target_folder}")
        return "/".join(parts)

    def _target_path_for(self, source: Path) -> Optional[Path]:
        relative = self.scan(source).relative_path
        rewritten = self._rewrite_relative(relative)
        if rewritten == relative:
            return None
        return self.source_root / rewritten

    def plan_destination(self, source: Path, output_root: Path) -> Path:
        """目录名与文件名都要改写 —— 内核默认的「源相对路径」在这里是错的。"""
        return Path(output_root) / self._rewrite_relative(
            self.scan(source).relative_path
        )

    # ----------------------------------------------------------------- render

    def render(
        self, units: Sequence[TranslationUnit], source: Path, destination: Path
    ) -> RenderResult:
        source = Path(source).resolve()
        destination = Path(destination)
        _language, entries = self._parse(source)
        by_key = {unit.logical_key: unit for unit in units}
        _source_folder, target_folder = self._folder_names()
        header_language = target_folder or self.target_locale

        lines = [f"l_{header_language}:"]
        for entry in entries:
            if "error" in entry:
                # 畸形行原样保留：它不是我们能安全改写的东西，丢掉等于静默损坏。
                lines.append(entry["raw"])
                continue
            unit = by_key.get(entry["key"])
            value = (
                unit.translation
                if unit is not None and unit.translation is not None
                else entry["value"]
            )
            version = entry["version"]
            lines.append(
                f"{entry['indent']}{entry['key']}:{version} "
                f"{_quote(value)}{entry['trailer']}"
            )

        # Paradox 要求 UTF-8 **带 BOM**，否则游戏读不出非 ASCII 字符。
        AtomicIO.write_text(destination, "\n".join(lines) + "\n", encoding="utf-8-sig")
        return RenderResult(
            destination=destination,
            unit_count=len(by_key),
            validation=self.validate(destination),
        )

    # --------------------------------------------------------------- validate

    def validate(self, path: Path) -> ValidationResult:
        path = Path(path)
        if not path.is_file():
            return ValidationResult(False, (f"missing file: {path}",))
        try:
            raw = path.read_bytes()
        except OSError as exc:
            return ValidationResult(False, (str(exc),))
        errors = []
        if not raw.startswith(b"\xef\xbb\xbf"):
            errors.append("Paradox localisation files must be UTF-8 with BOM")
        language, entries = self._parse(path)
        if not language:
            errors.append("missing l_<language> header on the first line")
        for entry in entries:
            if "error" in entry:
                errors.append(f"line {entry['line']}: unparsable entry")
        return ValidationResult(not errors, tuple(errors))
