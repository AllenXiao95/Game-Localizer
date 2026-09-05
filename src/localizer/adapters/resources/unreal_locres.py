from __future__ import annotations

import io
import json
import struct
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

from localizer.adapters.resources.options import AdapterOptionsModel, normalize_options
from localizer.adapters.resources.registry import register_adapter
from localizer.domain.translation_unit import TranslationUnit
from localizer.infrastructure.atomic_io import AtomicIO
from localizer.ports.resource import RenderResult, ResourceDescriptor, ValidationResult
from localizer.rules.placeholder import register_placeholder_syntax


LOCRES_MAGIC = bytes.fromhex("0e147475674a03fc4a15909dc3377f1b")
LOCRES_VERSION_OPTIMIZED_CITYHASH64_UTF16 = 3

# Unreal RichText allows a shorthand close tag (`</>`), which the generic
# `</?[A-Za-z]...>` pattern intentionally does not match.
UNREAL_LOCRES_PLACEHOLDERS: Tuple[str, ...] = (
    r"</>",
)

register_placeholder_syntax("unreal_locres", UNREAL_LOCRES_PLACEHOLDERS)


@dataclass(frozen=True)
class _LocresEntry:
    key_hash: int
    key: str
    source_string_hash: int
    value: str


@dataclass(frozen=True)
class _LocresNamespace:
    namespace_hash: int
    name: str
    entries: Tuple[_LocresEntry, ...]


@dataclass(frozen=True)
class _LocresV3:
    namespaces: Tuple[_LocresNamespace, ...]

    @property
    def entry_count(self) -> int:
        return sum(len(namespace.entries) for namespace in self.namespaces)


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def seek(self, offset: int) -> None:
        if offset < 0 or offset > len(self.data):
            raise ValueError(f"locres offset out of bounds: {offset}")
        self.pos = offset

    def read(self, size: int) -> bytes:
        if size < 0 or self.pos + size > len(self.data):
            raise ValueError("truncated locres")
        value = self.data[self.pos : self.pos + size]
        self.pos += size
        return value

    def u8(self) -> int:
        return self.read(1)[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.read(4))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.read(4))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self.read(8))[0]

    def fstring(self) -> str:
        length = self.i32()
        if length == 0:
            return ""
        if length > 0:
            raw = self.read(length)
            if not raw.endswith(b"\x00"):
                raise ValueError("ANSI FString is missing its terminator")
            try:
                return raw[:-1].decode("ascii")
            except UnicodeDecodeError as exc:
                raise ValueError("locres ANSI FString is not ASCII") from exc

        code_units = -length
        raw = self.read(code_units * 2)
        if not raw.endswith(b"\x00\x00"):
            raise ValueError("UTF-16 FString is missing its terminator")
        try:
            return raw[:-2].decode("utf-16-le")
        except UnicodeDecodeError as exc:
            raise ValueError("invalid UTF-16 FString") from exc


def _write_fstring(stream: io.BytesIO, value: str) -> None:
    if "\x00" in value:
        raise ValueError("locres strings cannot contain embedded NUL")
    if all(ord(char) <= 0x7F for char in value):
        payload = value.encode("ascii") + b"\x00"
        stream.write(struct.pack("<i", len(payload)))
        stream.write(payload)
        return

    payload = value.encode("utf-16-le") + b"\x00\x00"
    code_units = len(payload) // 2
    stream.write(struct.pack("<i", -code_units))
    stream.write(payload)


def _load_locres_v3(data: bytes) -> _LocresV3:
    reader = _Reader(data)
    if reader.read(len(LOCRES_MAGIC)) != LOCRES_MAGIC:
        raise ValueError("unsupported legacy locres: missing magic")

    version = reader.u8()
    if version != LOCRES_VERSION_OPTIMIZED_CITYHASH64_UTF16:
        raise ValueError(
            f"unsupported locres version {version}; unreal_locres v1 supports version 3"
        )

    string_table_offset = reader.i64()
    entry_count = reader.i32()
    namespace_count = reader.i32()
    if entry_count < 0 or namespace_count < 0:
        raise ValueError("locres contains negative counts")

    namespace_headers = []
    actual_entries = 0
    for _ in range(namespace_count):
        namespace_hash = reader.u32()
        namespace = reader.fstring()
        key_count = reader.i32()
        if key_count < 0:
            raise ValueError("locres namespace contains a negative key count")
        entries = []
        for _ in range(key_count):
            key_hash = reader.u32()
            key = reader.fstring()
            source_string_hash = reader.u32()
            string_index = reader.i32()
            entries.append((key_hash, key, source_string_hash, string_index))
            actual_entries += 1
        namespace_headers.append((namespace_hash, namespace, entries))

    if actual_entries != entry_count:
        raise ValueError(
            f"locres entry count mismatch: header={entry_count}, parsed={actual_entries}"
        )
    if reader.pos != string_table_offset:
        raise ValueError(
            "locres v3 string-table offset does not match the end of the key table"
        )

    reader.seek(string_table_offset)
    string_count = reader.i32()
    if string_count < 0:
        raise ValueError("locres contains a negative string-table count")
    strings = []
    ref_counts = []
    for _ in range(string_count):
        strings.append(reader.fstring())
        ref_count = reader.i32()
        if ref_count < 0:
            raise ValueError("locres contains a negative string reference count")
        ref_counts.append(ref_count)

    if reader.pos != len(data):
        raise ValueError("locres has unexpected trailing bytes")

    actual_ref_counts = [0] * string_count
    namespaces = []
    seen_coordinates = set()
    for namespace_hash, namespace, raw_entries in namespace_headers:
        entries = []
        for key_hash, key, source_string_hash, string_index in raw_entries:
            if string_index < 0 or string_index >= string_count:
                raise ValueError(f"locres string index out of range: {string_index}")
            coordinate = (namespace, key)
            if coordinate in seen_coordinates:
                raise ValueError(f"duplicate locres coordinate: {coordinate!r}")
            seen_coordinates.add(coordinate)
            actual_ref_counts[string_index] += 1
            entries.append(
                _LocresEntry(
                    key_hash=key_hash,
                    key=key,
                    source_string_hash=source_string_hash,
                    value=strings[string_index],
                )
            )
        namespaces.append(
            _LocresNamespace(
                namespace_hash=namespace_hash,
                name=namespace,
                entries=tuple(entries),
            )
        )

    if actual_ref_counts != ref_counts:
        raise ValueError("locres string-table reference counts are inconsistent")

    return _LocresV3(tuple(namespaces))


def _dump_locres_v3(resource: _LocresV3) -> bytes:
    stream = io.BytesIO()
    stream.write(LOCRES_MAGIC)
    stream.write(bytes((LOCRES_VERSION_OPTIMIZED_CITYHASH64_UTF16,)))
    string_table_offset_pos = stream.tell()
    stream.write(struct.pack("<q", 0))
    stream.write(struct.pack("<i", resource.entry_count))
    stream.write(struct.pack("<i", len(resource.namespaces)))

    string_indexes = {}
    string_values = []
    string_ref_counts = []

    for namespace in resource.namespaces:
        stream.write(struct.pack("<I", namespace.namespace_hash))
        _write_fstring(stream, namespace.name)
        stream.write(struct.pack("<i", len(namespace.entries)))
        for entry in namespace.entries:
            stream.write(struct.pack("<I", entry.key_hash))
            _write_fstring(stream, entry.key)
            stream.write(struct.pack("<I", entry.source_string_hash))

            string_index = string_indexes.get(entry.value)
            if string_index is None:
                string_index = len(string_values)
                string_indexes[entry.value] = string_index
                string_values.append(entry.value)
                string_ref_counts.append(0)
            string_ref_counts[string_index] += 1
            stream.write(struct.pack("<i", string_index))

    string_table_offset = stream.tell()
    stream.write(struct.pack("<i", len(string_values)))
    for value, ref_count in zip(string_values, string_ref_counts):
        _write_fstring(stream, value)
        stream.write(struct.pack("<i", ref_count))

    end = stream.tell()
    stream.seek(string_table_offset_pos)
    stream.write(struct.pack("<q", string_table_offset))
    stream.seek(end)
    return stream.getvalue()


def _logical_key(namespace: str, key: str) -> str:
    return json.dumps([namespace, key], ensure_ascii=False, separators=(",", ":"))


def _decode_logical_key(value: str) -> Tuple[str, str]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid unreal_locres logical_key: {value!r}") from exc
    if (
        not isinstance(decoded, list)
        or len(decoded) != 2
        or not all(isinstance(item, str) for item in decoded)
    ):
        raise ValueError(f"invalid unreal_locres logical_key: {value!r}")
    return decoded[0], decoded[1]


@register_adapter
class UnrealLocresAdapter:
    adapter_id = "unreal_locres"
    options_model = AdapterOptionsModel

    def __init__(
        self,
        *,
        project_id: str,
        source_root: Path,
        source_locale: str,
        target_locale: str,
        options: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.options = normalize_options(self.options_model, options)
        self.project_id = project_id
        self.source_root = Path(source_root).resolve()
        self.source_locale = source_locale
        self.target_locale = target_locale

    def probe(self, path: Path) -> float:
        candidate = Path(path)
        if candidate.suffix.lower() != ".locres" or not candidate.is_file():
            return 0.0
        try:
            _load_locres_v3(AtomicIO.read_bytes(candidate))
        except (OSError, ValueError):
            return 0.0
        return 1.0

    def scan(self, path: Path) -> ResourceDescriptor:
        candidate = Path(path).resolve(strict=True)
        confidence = self.probe(candidate)
        if confidence == 0:
            raise ValueError(f"not a supported Unreal locres v3 resource: {candidate}")
        return ResourceDescriptor(
            self.adapter_id,
            candidate,
            self._relative(candidate),
            candidate.stat().st_size,
            confidence,
        )

    def extract(self, path: Path) -> Sequence[TranslationUnit]:
        candidate = Path(path).resolve(strict=True)
        resource = _load_locres_v3(AtomicIO.read_bytes(candidate))
        relative = self._relative(candidate)
        units = []
        for namespace in resource.namespaces:
            for entry in namespace.entries:
                units.append(
                    TranslationUnit(
                        project_id=self.project_id,
                        adapter_id=self.adapter_id,
                        relative_path=relative,
                        logical_file=relative,
                        logical_key=_logical_key(namespace.name, entry.key),
                        source_text=entry.value,
                        translation=None,
                        context=f"{namespace.name} / {entry.key}"
                        if namespace.name
                        else entry.key,
                        source_locale=self.source_locale,
                        target_locale=self.target_locale,
                        metadata={
                            "namespace": namespace.name,
                            "key": entry.key,
                            "locres_version": LOCRES_VERSION_OPTIMIZED_CITYHASH64_UTF16,
                            "source_string_hash": entry.source_string_hash,
                            "namespace_hash": namespace.namespace_hash,
                            "key_hash": entry.key_hash,
                        },
                    )
                )
        return tuple(units)

    def render(
        self, units: Sequence[TranslationUnit], source: Path, destination: Path
    ) -> RenderResult:
        source_path = Path(source).resolve(strict=True)
        resource = _load_locres_v3(AtomicIO.read_bytes(source_path))

        source_entries = {
            (namespace.name, entry.key)
            for namespace in resource.namespaces
            for entry in namespace.entries
        }
        translations = {}
        for unit in units:
            coordinate = _decode_logical_key(unit.logical_key)
            if coordinate not in source_entries:
                raise ValueError(
                    f"unreal_locres unit does not exist in source: {coordinate!r}"
                )
            if coordinate in translations:
                raise ValueError(f"duplicate unreal_locres unit: {coordinate!r}")
            if unit.translation is not None:
                translations[coordinate] = unit.translation

        namespaces = []
        for namespace in resource.namespaces:
            entries = []
            for entry in namespace.entries:
                value = translations.get((namespace.name, entry.key), entry.value)
                entries.append(replace(entry, value=value))
            namespaces.append(replace(namespace, entries=tuple(entries)))

        destination_path = AtomicIO.write_bytes(
            Path(destination), _dump_locres_v3(_LocresV3(tuple(namespaces)))
        )
        validation = self.validate(destination_path)
        return RenderResult(destination_path, len(units), validation)

    def validate(self, path: Path) -> ValidationResult:
        try:
            _load_locres_v3(AtomicIO.read_bytes(Path(path)))
        except (OSError, ValueError) as exc:
            return ValidationResult(False, (str(exc),))
        return ValidationResult(True)

    def _relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.source_root).as_posix()
        except ValueError as exc:
            raise ValueError(
                f"resource is outside source root {self.source_root}: {path}"
            ) from exc
