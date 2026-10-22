from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from localizer.config.models import TokenizerSection
from localizer.infrastructure.token_counting import HuggingFaceTokenCounter


class TokenizerRevisionTests(unittest.TestCase):
    def test_blank_revision_uses_hub_default_instead_of_empty_url_segment(self) -> None:
        for revision in (None, "", " \t "):
            with self.subTest(revision=revision), tempfile.TemporaryDirectory() as temp:
                calls = []

                def from_pretrained(source, **kwargs):
                    calls.append((source, kwargs))
                    # The Hub only defaults None to main; '' produces resolve//.
                    self.assertIsNone(kwargs["revision"])
                    return types.SimpleNamespace(
                        encode=lambda text, **options: text.split()
                    )

                module = types.SimpleNamespace(
                    AutoTokenizer=types.SimpleNamespace(from_pretrained=from_pretrained)
                )
                config = TokenizerSection(model="vendor/new-model", revision=revision)
                with mock.patch.dict(sys.modules, {"transformers": module}):
                    counter = HuggingFaceTokenCounter(config, Path(temp))
                    counter.warm_up()
                    self.assertEqual(2, counter("one two"))
                self.assertEqual(1, len(calls))
                self.assertEqual("vendor/new-model", calls[0][0])

    def test_revision_is_trimmed_without_changing_explicit_identity(self) -> None:
        for revision in ("main", "release/v1", "a" * 40):
            with self.subTest(revision=revision):
                config = TokenizerSection(
                    model="vendor/new-model", revision=f" {revision}\t"
                )
                self.assertEqual(revision, config.revision)

    def test_blank_revision_resolves_main_cache_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cache = Path(temp)
            repository = cache / "models--vendor--new-model"
            commit = "a" * 40
            snapshot = repository / "snapshots" / commit
            snapshot.mkdir(parents=True)
            (snapshot / "tokenizer_config.json").write_text("{}", encoding="utf-8")
            (snapshot / "tokenizer.json").write_text("{}", encoding="utf-8")
            (repository / "refs").mkdir()
            (repository / "refs" / "main").write_text(commit, encoding="utf-8")
            load = mock.Mock(return_value=object())
            module = types.SimpleNamespace(
                AutoTokenizer=types.SimpleNamespace(from_pretrained=load)
            )
            with mock.patch.dict(sys.modules, {"transformers": module}):
                counter = HuggingFaceTokenCounter(
                    TokenizerSection(
                        model="vendor/new-model", revision=" \t ", local_files_only=True
                    ),
                    cache,
                )
                counter.warm_up()
            load.assert_called_once_with(str(snapshot.resolve()), local_files_only=True)


if __name__ == "__main__":
    unittest.main()
