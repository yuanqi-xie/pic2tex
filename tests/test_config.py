"""Tests for config handling (Lead-owned)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pic2tex.config import Config, app_data_dir, default_config_path


class ConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        config = Config()
        self.assertEqual(config.effective_model, "deepseek-flash")
        self.assertEqual(config.effective_base_url, "https://api.deepseek.com")
        self.assertEqual(config.validate(), [])

    def test_save_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "config.json"
            config = Config(api_key="sk-test", prompt_style="document", max_tokens=1024)
            config.save(path)
            self.assertTrue(path.exists())
            loaded = Config.load(path)
            self.assertEqual(loaded.api_key, "sk-test")
            self.assertEqual(loaded.prompt_style, "document")
            self.assertEqual(loaded.max_tokens, 1024)

    def test_corrupt_file_falls_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{not json", encoding="utf-8")
            loaded = Config.load(path)
            self.assertEqual(loaded.model, "deepseek-flash")

    def test_unknown_keys_ignored(self) -> None:
        config = Config.from_dict({"api_key": "k", "nonsense": 1, "timeout": "60"})
        self.assertEqual(config.api_key, "k")
        self.assertEqual(config.timeout, 60.0)

    def test_env_overrides(self) -> None:
        config = Config(api_key="file-key", model="file-model")
        with mock.patch.dict(
            os.environ,
            {
                "DEEPSEEK_API_KEY": "env-key",
                "DEEPSEEK_BASE_URL": "https://example.com/v1/",
                "PIC2TEX_MODEL": "env-model",
            },
        ):
            self.assertEqual(config.effective_api_key, "env-key")
            self.assertEqual(config.effective_base_url, "https://example.com/v1")
            self.assertEqual(config.effective_model, "env-model")
            self.assertEqual(config.api_key_source, "环境变量")

    def test_validation_rejects_bad_values(self) -> None:
        config = Config(prompt_style="nope", wrapper="nope", timeout=1, max_tokens=10)
        problems = config.validate()
        self.assertGreaterEqual(len(problems), 3)

    def test_masked_key(self) -> None:
        self.assertEqual(Config(api_key="sk-1234567890").masked_key(), "sk-1…7890")
        self.assertEqual(Config().masked_key(), "未配置")

    def test_resolved_save_dir_default(self) -> None:
        self.assertIn("Documents", str(Config().resolved_save_dir()))

    def test_default_paths(self) -> None:
        self.assertTrue(str(default_config_path()).endswith("config.json"))
        self.assertEqual(app_data_dir().name, "Pic2TeX")


if __name__ == "__main__":
    unittest.main()
