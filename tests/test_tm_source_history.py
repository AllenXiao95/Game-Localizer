from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from localizer.adapters.storage.sqlite_tm import SQLiteTranslationMemory, TMEntry


class TMSourceHistoryTests(unittest.TestCase):
    """A newer source branch must not erase the exact baseline of a sibling branch."""

    @staticmethod
    def _entry(
        fingerprint: str,
        translation: str,
        *,
        run_id: str,
        formal: bool = True,
    ) -> TMEntry:
        return TMEntry(
            stable_identity="demo:gettext:ui/menu.po:start",
            project_id="demo",
            adapter_id="gettext",
            relative_path="ui/menu.po",
            logical_key="start",
            source_text=f"source-{fingerprint}",
            source_fingerprint=fingerprint,
            translation=translation,
            origin="machine",
            review_state="unreviewed",
            match_scope="coordinate_exact",
            run_id=run_id,
            quality_state="passed",
            is_formal=formal,
        )

    def test_retired_formal_remains_reusable_by_exact_source_fingerprint(self) -> None:
        """RU -> PT -> RU must recover RU's exact coordinate state, not PT's latest row."""
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "tm.sqlite"
            ru_1440 = self._entry("ru-1.44.0", "RU translation", run_id="ru-1440")
            pt_1450 = self._entry("pt-1.45.0", "PT translation", run_id="pt-1450")

            with SQLiteTranslationMemory(database) as tm:
                tm.upsert(ru_1440)
                self.assertEqual(
                    1,
                    tm.retire_stale_formal_entries(
                        [pt_1450], expected_identities=[ru_1440.stable_identity]
                    ),
                )
                tm.upsert(pt_1450)

                current = tm.lookup(
                    ru_1440.stable_identity,
                    source_fingerprint=pt_1450.source_fingerprint,
                    allow_shadow=True,
                )
                previous = tm.lookup(
                    ru_1440.stable_identity,
                    source_fingerprint=ru_1440.source_fingerprint,
                    allow_shadow=True,
                )

            self.assertIsNotNone(current)
            self.assertEqual("PT translation", current.translation)
            self.assertIsNotNone(previous)
            self.assertEqual("RU translation", previous.translation)
            self.assertEqual("coordinate_history", previous.match_scope)

    def test_exact_history_prevents_false_stale_on_sibling_branch_return(self) -> None:
        """A historical exact hit needs no write, so the current PT formal must not block RU."""
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "tm.sqlite"
            ru_1440 = self._entry("ru-1.44.0", "RU translation", run_id="ru-1440")
            pt_1450 = self._entry("pt-1.45.0", "PT translation", run_id="pt-1450")
            ru_return = self._entry(
                "ru-1.44.0", "", run_id="ru-1441", formal=False
            )

            with SQLiteTranslationMemory(database) as tm:
                tm.upsert(ru_1440)
                tm.retire_stale_formal_entries(
                    [pt_1450], expected_identities=[ru_1440.stable_identity]
                )
                tm.upsert(pt_1450)

                stale = tm.stale_formal_identities([ru_return])
                historical = tm.lookup(
                    ru_return.stable_identity,
                    source_fingerprint=ru_return.source_fingerprint,
                    allow_shadow=True,
                )

            self.assertEqual((), stale)
            self.assertIsNotNone(historical)
            self.assertEqual("RU translation", historical.translation)
            self.assertEqual("coordinate_history", historical.match_scope)

    def test_unseen_source_still_requires_normal_stale_handling(self) -> None:
        """History is an exact baseline only; it must not hide a real new RU patch."""
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "tm.sqlite"
            ru_1440 = self._entry("ru-1.44.0", "RU translation", run_id="ru-1440")
            pt_1450 = self._entry("pt-1.45.0", "PT translation", run_id="pt-1450")
            ru_1441_changed = self._entry(
                "ru-1.44.1-changed", "", run_id="ru-1441", formal=False
            )

            with SQLiteTranslationMemory(database) as tm:
                tm.upsert(ru_1440)
                tm.retire_stale_formal_entries(
                    [pt_1450], expected_identities=[ru_1440.stable_identity]
                )
                tm.upsert(pt_1450)

                stale = tm.stale_formal_identities([ru_1441_changed])
                missing = tm.lookup(
                    ru_1441_changed.stable_identity,
                    source_fingerprint=ru_1441_changed.source_fingerprint,
                    allow_shadow=True,
                )

            self.assertEqual((ru_1441_changed.stable_identity,), stale)
            self.assertIsNone(missing)


    def test_retirement_excludes_exact_history_even_with_full_plan_entries(self) -> None:
        """One new source is stale; a sibling branch returning to history is not."""
        with tempfile.TemporaryDirectory() as temp:
            with SQLiteTranslationMemory(Path(temp) / "tm.sqlite") as tm:
                old_a = self._entry("a-old", "A old", run_id="a-old")
                new_a = self._entry("a-new", "", run_id="a-new", formal=False)
                old_b = replace(
                    self._entry("b-old", "B old", run_id="b-old"),
                    stable_identity="demo:gettext:ui/menu.po:other",
                    logical_key="other",
                )
                current_b = replace(
                    self._entry("b-current", "B current", run_id="b-current"),
                    stable_identity=old_b.stable_identity,
                    logical_key="other",
                )
                tm.upsert(old_a)
                tm.upsert(old_b)
                tm.retire_stale_formal_entries(
                    [current_b], expected_identities=[old_b.stable_identity]
                )
                tm.upsert(current_b)

                preview = tm.stale_formal_identities([new_a, old_b])
                self.assertEqual((old_a.stable_identity,), preview)
                before = tm.rows_for(preview)
                self.assertEqual(
                    1,
                    tm.retire_stale_formal_entries(
                        [new_a, old_b], expected_identities=preview,
                        expected_rows={
                            key: tm.row_revision_token(row)
                            for key, row in before.items()
                        },
                    ),
                )
                self.assertNotIn(
                    old_a.stable_identity, tm.rows_for([old_a.stable_identity])
                )
                self.assertEqual(
                    current_b.source_fingerprint,
                    tm.rows_for([old_b.stable_identity])[old_b.stable_identity][
                        "source_fingerprint"
                    ],
                )
                self.assertEqual(
                    "B old",
                    tm.lookup(old_b.stable_identity,
                              source_fingerprint=old_b.source_fingerprint).translation,
                )
                self.assertEqual(0, tm.retire_stale_formal_entries([old_b]))

    def test_same_count_wrong_identity_and_empty_confirmation_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with SQLiteTranslationMemory(Path(temp) / "tm.sqlite") as tm:
                old_a = self._entry("old", "old", run_id="old")
                new_a = self._entry("new", "", run_id="new", formal=False)
                tm.upsert(old_a)

                for approved in ([old_a.stable_identity + "-wrong"], []):
                    with self.subTest(approved=approved):
                        with self.assertRaisesRegex(RuntimeError, "candidates changed"):
                            tm.retire_stale_formal_entries(
                                [new_a], expected_identities=approved
                            )
                        self.assertIn(
                            old_a.stable_identity,
                            tm.rows_for([old_a.stable_identity]),
                        )
                        self.assertEqual(
                            0, tm.connection.execute(
                                "SELECT COUNT(*) FROM tm_source_history"
                            ).fetchone()[0],
                        )

    def test_changed_before_image_refused_even_if_fingerprint_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with SQLiteTranslationMemory(Path(temp) / "tm.sqlite") as tm:
                old = self._entry("old", "translation 1", run_id="old")
                new = self._entry("new", "", run_id="new", formal=False)
                tm.upsert(old)
                old_token = tm.row_revision_token(
                    tm.rows_for([old.stable_identity])[old.stable_identity]
                )
                tm.upsert(replace(old, translation="translation 2"))
                with self.assertRaisesRegex(RuntimeError, "changed since confirmation"):
                    tm.retire_stale_formal_entries(
                        [new], expected_identities=[old.stable_identity],
                        expected_rows={old.stable_identity: old_token},
                    )
                self.assertEqual(
                    "translation 2",
                    tm.rows_for([old.stable_identity])[old.stable_identity][
                        "translation"
                    ],
                )

    def test_late_snapshot_failure_rolls_back_earlier_archive_and_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with SQLiteTranslationMemory(Path(temp) / "tm.sqlite") as tm:
                old_a = self._entry("a-old", "A old", run_id="a-old")
                old_b = replace(
                    self._entry("b-old", "B old", run_id="b-old"),
                    stable_identity="demo:gettext:ui/menu.po:z",
                    logical_key="z",
                )
                new_a = replace(old_a, source_fingerprint="a-new", is_formal=False)
                new_b = replace(old_b, source_fingerprint="b-new", is_formal=False)
                tm.upsert(old_a)
                tm.upsert(old_b)
                before = tm.rows_for([old_a.stable_identity, old_b.stable_identity])
                tokens = {
                    key: tm.row_revision_token(row) for key, row in before.items()
                }
                tokens[old_b.stable_identity] = "wrong-token"
                with self.assertRaisesRegex(RuntimeError, "changed since confirmation"):
                    tm.retire_stale_formal_entries(
                        [new_a, new_b],
                        expected_identities=[old_a.stable_identity, old_b.stable_identity],
                        expected_rows=tokens,
                    )
                self.assertEqual(before, tm.rows_for(list(before)))
                self.assertEqual(
                    0, tm.connection.execute(
                        "SELECT COUNT(*) FROM tm_source_history"
                    ).fetchone()[0],
                )

    def test_missing_confirmed_candidate_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with SQLiteTranslationMemory(Path(temp) / "tm.sqlite") as tm:
                old = self._entry("old", "old", run_id="old")
                tm.upsert(old)
                with self.assertRaisesRegex(RuntimeError, "candidates changed"):
                    tm.retire_stale_formal_entries(
                        [], expected_identities=[old.stable_identity]
                    )
                self.assertIn(old.stable_identity, tm.rows_for([old.stable_identity]))


if __name__ == "__main__":
    unittest.main()
