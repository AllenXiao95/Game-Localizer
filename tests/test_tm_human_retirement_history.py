from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from localizer.adapters.storage.sqlite_tm import (
    HUMAN_REVIEW_FIELDS,
    SQLiteTranslationMemory,
    TMEntry,
)


class TMHumanRetirementHistoryTests(unittest.TestCase):
    @staticmethod
    def _entry(
        fingerprint: str,
        translation: str,
        *,
        run_id: str,
        origin: str = "machine",
        human: bool = False,
    ) -> TMEntry:
        kwargs = dict(HUMAN_REVIEW_FIELDS) if human else {
            "origin": origin,
            "review_state": "unreviewed",
            "quality_state": "passed",
            "classification": "native",
            "match_scope": "coordinate_exact",
            "is_formal": True,
            "human_authored": False,
        }
        return TMEntry(
            stable_identity="demo:gettext:ui/menu.po:start",
            project_id="demo",
            adapter_id="gettext",
            relative_path="ui/menu.po",
            logical_key="start",
            source_text=f"source-{fingerprint}",
            source_fingerprint=fingerprint,
            translation=translation,
            run_id=run_id,
            **kwargs,
        )

    def test_explicit_human_retirement_cannot_resurrect_same_fingerprint_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "tm.sqlite"
            source_a = self._entry("source-a", "machine A", run_id="a-machine")
            source_b = self._entry("source-b", "machine B", run_id="b-machine")
            human_a = self._entry(
                "source-a", "human A", run_id="a-human", human=True
            )

            with SQLiteTranslationMemory(database) as tm:
                # A -> B archives A. Returning to A with a human decision leaves the older
                # machine A row in exact source history, which is the dangerous fallback.
                tm.upsert(source_a)
                tm.upsert(source_b)
                tm.apply_human_review([human_a])

                current_a = tm.lookup(
                    human_a.stable_identity,
                    source_fingerprint=human_a.source_fingerprint,
                    allow_shadow=True,
                )
                self.assertIsNotNone(current_a)
                self.assertEqual("human A", current_a.translation)

                self.assertEqual(1, tm.retire_human_entries([human_a.stable_identity]))

                # "I do not want this human decision anymore" means this exact source state
                # must become pending; an older historical machine translation cannot revive it.
                self.assertIsNone(
                    tm.lookup(
                        human_a.stable_identity,
                        source_fingerprint=human_a.source_fingerprint,
                        allow_shadow=True,
                    )
                )

                # The retirement is scoped to the current source fingerprint only. Sibling
                # branch history remains reusable for branch-safe source-state round trips.
                historical_b = tm.lookup(
                    human_a.stable_identity,
                    source_fingerprint=source_b.source_fingerprint,
                    allow_shadow=True,
                )
                self.assertIsNotNone(historical_b)
                self.assertEqual("machine B", historical_b.translation)
                self.assertEqual("coordinate_history", historical_b.match_scope)


if __name__ == "__main__":
    unittest.main()
