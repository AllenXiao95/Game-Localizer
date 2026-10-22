from __future__ import annotations

import fnmatch
import os
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from localizer.config.models import TokenizerSection
from localizer.infrastructure.token_counting import HuggingFaceTokenCounter


class TokenizerDownloadFallbackTests(unittest.TestCase):
    @contextmanager
    def runtime(self, *, endpoint="https://hf-mirror.com", hub_offline=False):
        class HTTPError(Exception):
            pass

        load = mock.Mock()
        download = mock.Mock()
        constants = types.SimpleNamespace(
            ENDPOINT=endpoint, HF_HUB_OFFLINE=hub_offline
        )
        modules = {
            "httpx": types.SimpleNamespace(HTTPError=HTTPError),
            "transformers": types.SimpleNamespace(
                AutoTokenizer=types.SimpleNamespace(from_pretrained=load)
            ),
            "huggingface_hub": types.SimpleNamespace(
                constants=constants, snapshot_download=download
            ),
        }
        # Simulate a Hub imported before a task changes its environment.
        env = {
            "HF_ENDPOINT": "https://different-task.example",
            "HF_HUB_OFFLINE": "0",
            "TRANSFORMERS_OFFLINE": "0",
        }
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            sys.modules, modules
        ), mock.patch.dict(os.environ, env):
            cache = Path(temp)
            yield cache, load, download, constants

    def test_mirror_failure_downloads_exact_revision_and_reuses_local_cache(self):
        with self.runtime() as (cache, load, download, constants):
            config = TokenizerSection(model="vendor/new-model", revision="a" * 40)
            snapshot = cache / "models--vendor--new-model" / "snapshots" / config.revision
            tokenizer = types.SimpleNamespace(encode=lambda text, **kwargs: text.split())

            def fetch(**kwargs):
                snapshot.mkdir(parents=True)
                (snapshot / "tokenizer_config.json").write_text("{}", encoding="utf-8")
                (snapshot / "tokenizer.json").write_text("{}", encoding="utf-8")
                return str(snapshot)

            download.side_effect = fetch
            load.side_effect = [OSError("mirror returned HTTP 308"), tokenizer]
            counter = HuggingFaceTokenCounter(config, cache)
            with self.assertLogs("localizer.infrastructure.token_counting", "WARNING"):
                counter.warm_up()
            self.assertEqual(2, counter("one two"))
            self.assertEqual(snapshot.resolve(), counter.resolved_source)
            self.assertEqual(
                [
                    mock.call(
                        config.model, revision=config.revision,
                        cache_dir=str(cache), local_files_only=False,
                    ),
                    mock.call(str(snapshot), local_files_only=True),
                ],
                load.call_args_list,
            )
            kwargs = download.call_args.kwargs
            self.assertEqual(config.model, kwargs["repo_id"])
            self.assertEqual(config.revision, kwargs["revision"])
            self.assertEqual(str(cache), kwargs["cache_dir"])
            self.assertEqual("https://huggingface.co", kwargs["endpoint"])
            for filename in ["tokenizer.json", "tokenizer_config.json", "vocab.json", "merges.txt"]:
                self.assertTrue(any(fnmatch.fnmatch(filename, p) for p in kwargs["allow_patterns"]))
            for filename in ["model.safetensors", "pytorch_model.bin", "model-00001-of-00100.safetensors"]:
                self.assertFalse(any(fnmatch.fnmatch(filename, p) for p in kwargs["allow_patterns"]))
            self.assertEqual("https://hf-mirror.com", constants.ENDPOINT)
            self.assertEqual("https://different-task.example", os.environ["HF_ENDPOINT"])

            # A new counter must use the downloaded snapshot even with downloads enabled.
            load.reset_mock(side_effect=True)
            load.return_value = tokenizer
            download.reset_mock()
            HuggingFaceTokenCounter(config, cache).warm_up()
            load.assert_called_once_with(str(snapshot.resolve()), local_files_only=True)
            download.assert_not_called()

    def test_unwrapped_httpx_transport_error_triggers_official_fallback(self):
        with self.runtime() as (cache, load, download, _):
            from httpx import HTTPError

            snapshot = cache / "official-snapshot"
            download.return_value = str(snapshot)
            tokenizer = object()
            load.side_effect = [HTTPError("proxy connection failed"), tokenizer]
            counter = HuggingFaceTokenCounter(
                TokenizerSection(model="vendor/new-model"), cache
            )
            with self.assertLogs("localizer.infrastructure.token_counting", "WARNING"):
                counter.warm_up()
            self.assertEqual(1, download.call_count)
            self.assertIsNone(download.call_args.kwargs["revision"])
            self.assertEqual(
                mock.call(str(snapshot), local_files_only=True), load.call_args
            )

    def test_both_failures_are_reported_without_retry_storm(self):
        with self.runtime() as (cache, load, download, _):
            load.side_effect = OSError("mirror unavailable")
            download.side_effect = OSError("official timed out")
            counter = HuggingFaceTokenCounter(
                TokenizerSection(model="vendor/new-model", revision="release/v2"), cache
            )
            with self.assertLogs("localizer.infrastructure.token_counting", "WARNING"):
                with self.assertRaises(RuntimeError) as caught:
                    counter.warm_up()
            message = str(caught.exception)
            for detail in [
                "mirror unavailable", "official timed out", "vendor/new-model",
                "release/v2", repr(str(cache)), "https://huggingface.co",
            ]:
                self.assertIn(detail, message)
            with self.assertRaises(RuntimeError):
                counter.warm_up()
            self.assertEqual(1, load.call_count)
            self.assertEqual(1, download.call_count)

    def test_strict_local_mode_never_attempts_a_download(self):
        with self.runtime() as (cache, load, download, _):
            counter = HuggingFaceTokenCounter(
                TokenizerSection(model="vendor/new-model", local_files_only=True), cache
            )
            with self.assertRaisesRegex(RuntimeError, "local_files_only=false"):
                counter.warm_up()
            load.assert_not_called()
            download.assert_not_called()

    def test_offline_environment_disables_network_and_official_fallback(self):
        for variable in ["HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"]:
            with self.subTest(variable=variable), self.runtime() as (cache, load, download, _):
                load.side_effect = OSError("offline cache miss")
                counter = HuggingFaceTokenCounter(TokenizerSection(model="vendor/new-model"), cache)
                with mock.patch.dict(os.environ, {variable: "yes"}):
                    with self.assertRaisesRegex(RuntimeError, "offline cache miss"):
                        counter.warm_up()
                self.assertTrue(load.call_args.kwargs["local_files_only"])
                download.assert_not_called()

    def test_imported_hub_offline_flag_disables_fallback(self):
        with self.runtime(hub_offline=True) as (cache, load, download, _):
            load.side_effect = OSError("offline cache miss")
            counter = HuggingFaceTokenCounter(TokenizerSection(model="vendor/new-model"), cache)
            with self.assertRaisesRegex(RuntimeError, "offline cache miss"):
                counter.warm_up()
            download.assert_not_called()

    def test_official_and_custom_endpoints_are_not_retried_elsewhere(self):
        for endpoint in ["https://huggingface.co", "https://private-hub.example", "https://hf-mirror.com.example"]:
            with self.subTest(endpoint=endpoint), self.runtime(endpoint=endpoint) as (cache, load, download, _):
                load.side_effect = OSError("repository not available")
                counter = HuggingFaceTokenCounter(TokenizerSection(model="vendor/new-model"), cache)
                with self.assertRaisesRegex(RuntimeError, "repository not available"):
                    counter.warm_up()
                download.assert_not_called()

    def test_tokenizer_parser_error_is_not_treated_as_network_failure(self):
        with self.runtime() as (cache, load, download, _):
            load.side_effect = ValueError("unsupported tokenizer format")
            counter = HuggingFaceTokenCounter(TokenizerSection(model="vendor/new-model"), cache)
            with self.assertRaisesRegex(RuntimeError, "unsupported tokenizer format"):
                counter.warm_up()
            download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
