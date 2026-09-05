from __future__ import annotations

from dataclasses import replace

from localizer.adapters.resources.registry import available_adapters
from localizer.adapters.resources.unreal_locres import (
    LOCRES_VERSION_OPTIMIZED_CITYHASH64_UTF16,
    UnrealLocresAdapter,
    _LocresEntry,
    _LocresNamespace,
    _LocresV3,
    _dump_locres_v3,
)
from localizer.rules.placeholder import PlaceholderRule


def _fixture() -> _LocresV3:
    return _LocresV3(
        (
            _LocresNamespace(
                namespace_hash=101,
                name="ST_A",
                entries=(
                    _LocresEntry(
                        key_hash=201,
                        key="Label",
                        source_string_hash=301,
                        value="<bold>{PlayerName}</>",
                    ),
                    _LocresEntry(
                        key_hash=202,
                        key="Plural",
                        source_string_hash=302,
                        value="Add {Quantity}|plural(one=Salute,other=Salutes)",
                    ),
                ),
            ),
            _LocresNamespace(
                namespace_hash=102,
                name="ST_B",
                entries=(
                    _LocresEntry(
                        key_hash=203,
                        key="Label",
                        source_string_hash=303,
                        value="Ability",
                    ),
                ),
            ),
        )
    )


def _adapter(tmp_path) -> UnrealLocresAdapter:
    return UnrealLocresAdapter(
        project_id="tyr",
        source_root=tmp_path,
        source_locale="en",
        target_locale="zh-Hans",
        options={},
    )


def _semantic(units):
    return tuple(
        (
            unit.logical_key,
            unit.source_text,
            unit.metadata["source_string_hash"],
            unit.metadata["namespace_hash"],
            unit.metadata["key_hash"],
        )
        for unit in units
    )


def test_unreal_locres_is_discovered_and_extracts_stable_namespace_key_identity(tmp_path):
    assert "unreal_locres" in available_adapters()

    source = tmp_path / "Tyr/Content/Localization/Game/en/Game.locres"
    source.parent.mkdir(parents=True)
    source.write_bytes(_dump_locres_v3(_fixture()))

    adapter = _adapter(tmp_path)
    assert adapter.probe(source) == 1.0
    assert adapter.scan(source).relative_path == "Tyr/Content/Localization/Game/en/Game.locres"

    units = adapter.extract(source)
    assert len(units) == 3
    assert units[0].logical_key == '["ST_A","Label"]'
    assert units[2].logical_key == '["ST_B","Label"]'
    assert units[0].stable_identity != units[2].stable_identity
    assert units[0].translation is None
    assert units[0].metadata["locres_version"] == LOCRES_VERSION_OPTIMIZED_CITYHASH64_UTF16


def test_unreal_locres_noop_render_is_byte_identical_and_semantically_equal(tmp_path):
    source = tmp_path / "source/Game.locres"
    source.parent.mkdir(parents=True)
    original = _dump_locres_v3(_fixture())
    source.write_bytes(original)

    adapter = _adapter(tmp_path)
    before = adapter.extract(source)
    destination = tmp_path / "rendered/Game.locres"
    result = adapter.render(before, source, destination)

    assert result.validation.valid
    assert destination.read_bytes() == original
    after = adapter.extract(destination)
    assert _semantic(after) == _semantic(before)


def test_unreal_locres_single_entry_mutation_preserves_unrelated_entries_and_hashes(tmp_path):
    source = tmp_path / "source/Game.locres"
    source.parent.mkdir(parents=True)
    source.write_bytes(_dump_locres_v3(_fixture()))

    adapter = _adapter(tmp_path)
    before = adapter.extract(source)
    changed = tuple(
        replace(unit, translation="<bold>{PlayerName}，你好</>")
        if unit.logical_key == '["ST_A","Label"]'
        else unit
        for unit in before
    )

    destination = tmp_path / "rendered/Game.locres"
    result = adapter.render(changed, source, destination)
    assert result.validation.valid

    after = adapter.extract(destination)
    differences = [
        (left, right)
        for left, right in zip(before, after)
        if left.source_text != right.source_text
    ]
    assert len(differences) == 1
    original, translated = differences[0]
    assert original.logical_key == translated.logical_key == '["ST_A","Label"]'
    assert translated.source_text == "<bold>{PlayerName}，你好</>"
    assert translated.metadata == original.metadata

    unchanged_before = [unit for unit in before if unit.logical_key != '["ST_A","Label"]']
    unchanged_after = [unit for unit in after if unit.logical_key != '["ST_A","Label"]']
    assert _semantic(unchanged_after) == _semantic(unchanged_before)


def test_unreal_locres_placeholder_profile_covers_richtext_shorthand_close():
    rule = PlaceholderRule.for_adapter("unreal_locres")
    assert rule.extract("<bold>{PlayerName}</>") == (
        "<bold>",
        "{PlayerName}",
        "</>",
    )
    assert rule.round_trip("<bold>{PlayerName}</>")


def test_unreal_locres_v1_fails_closed_on_other_versions(tmp_path):
    source = tmp_path / "Game.locres"
    payload = bytearray(_dump_locres_v3(_fixture()))
    payload[16] = 2
    source.write_bytes(payload)

    adapter = _adapter(tmp_path)
    assert adapter.probe(source) == 0.0
    validation = adapter.validate(source)
    assert not validation.valid
    assert "supports version 3" in validation.errors[0]
