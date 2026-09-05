from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

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


class UnrealLocresAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.adapter = UnrealLocresAdapter(
            project_id="tyr",
            source_root=self.root,
            source_locale="en",
            target_locale="zh-Hans",
            options={},
        )

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _write_fixture(self, relative: str = "source/Game.locres") -> Path:
        source = self.root / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(_dump_locres_v3(_fixture()))
        return source

    def test_is_discovered_and_extracts_stable_namespace_key_identity(self) -> None:
        self.assertIn("unreal_locres", available_adapters())
        source = self._write_fixture(
            "Tyr/Content/Localization/Game/en/Game.locres"
        )

        self.assertEqual(self.adapter.probe(source), 1.0)
        self.assertEqual(
            self.adapter.scan(source).relative_path,
            "Tyr/Content/Localization/Game/en/Game.locres",
        )

        units = self.adapter.extract(source)
        self.assertEqual(len(units), 3)
        self.assertEqual(units[0].logical_key, '["ST_A","Label"]')
        self.assertEqual(units[2].logical_key, '["ST_B","Label"]')
        self.assertNotEqual(units[0].stable_identity, units[2].stable_identity)
        self.assertIsNone(units[0].translation)
        self.assertEqual(
            units[0].metadata["locres_version"],
            LOCRES_VERSION_OPTIMIZED_CITYHASH64_UTF16,
        )

    def test_noop_render_is_byte_identical_and_semantically_equal(self) -> None:
        source = self._write_fixture()
        original = source.read_bytes()

        before = self.adapter.extract(source)
        destination = self.root / "rendered/Game.locres"
        result = self.adapter.render(before, source, destination)

        self.assertTrue(result.validation.valid)
        self.assertEqual(destination.read_bytes(), original)
        after = self.adapter.extract(destination)
        self.assertEqual(_semantic(after), _semantic(before))

    def test_single_entry_mutation_preserves_unrelated_entries_and_hashes(self) -> None:
        source = self._write_fixture()
        before = self.adapter.extract(source)
        changed = tuple(
            replace(unit, translation="<bold>{PlayerName}，你好</>")
            if unit.logical_key == '["ST_A","Label"]'
            else unit
            for unit in before
        )

        destination = self.root / "rendered/Game.locres"
        result = self.adapter.render(changed, source, destination)
        self.assertTrue(result.validation.valid)

        after = self.adapter.extract(destination)
        differences = [
            (left, right)
            for left, right in zip(before, after)
            if left.source_text != right.source_text
        ]
        self.assertEqual(len(differences), 1)
        original, translated = differences[0]
        self.assertEqual(
            original.logical_key,
            translated.logical_key,
        )
        self.assertEqual(translated.logical_key, '["ST_A","Label"]')
        self.assertEqual(translated.source_text, "<bold>{PlayerName}，你好</>")
        self.assertEqual(translated.metadata, original.metadata)

        unchanged_before = [
            unit for unit in before if unit.logical_key != '["ST_A","Label"]'
        ]
        unchanged_after = [
            unit for unit in after if unit.logical_key != '["ST_A","Label"]'
        ]
        self.assertEqual(_semantic(unchanged_after), _semantic(unchanged_before))

    def test_placeholder_profile_covers_richtext_shorthand_close(self) -> None:
        rule = PlaceholderRule.for_adapter("unreal_locres")
        self.assertEqual(
            rule.extract("<bold>{PlayerName}</>"),
            ("<bold>", "{PlayerName}", "</>"),
        )
        self.assertTrue(rule.round_trip("<bold>{PlayerName}</>"))

    def test_v1_fails_closed_on_other_versions(self) -> None:
        source = self.root / "Game.locres"
        payload = bytearray(_dump_locres_v3(_fixture()))
        payload[16] = 2
        source.write_bytes(payload)

        self.assertEqual(self.adapter.probe(source), 0.0)
        validation = self.adapter.validate(source)
        self.assertFalse(validation.valid)
        self.assertIn("supports version 3", validation.errors[0])


if __name__ == "__main__":
    unittest.main()
