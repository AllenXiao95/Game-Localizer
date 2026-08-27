from __future__ import annotations

import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
