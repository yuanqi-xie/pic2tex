"""CLI regression tests (Lead-owned)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class CliTests(unittest.TestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [sys.executable, "-m", "pic2tex", *args],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )

    def test_version(self) -> None:
        proc = self._run("--version")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("Pic2TeX", proc.stdout)

    def test_output_without_images_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "out.tex"
            proc = self._run("-o", str(target))
            self.assertEqual(proc.returncode, 2)
            self.assertIn("至少一个图片文件", proc.stderr)
            self.assertFalse(target.exists())

    def test_check_without_key(self) -> None:
        env = dict(os.environ)
        env.pop("DEEPSEEK_API_KEY", None)
        env["PYTHONIOENCODING"] = "utf-8"
        # Point the config at an isolated directory so a real user config is
        # never read by the test.
        with tempfile.TemporaryDirectory() as tmp:
            env["APPDATA"] = tmp
            proc = subprocess.run(
                [sys.executable, "-m", "pic2tex", "--check"],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
            )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("未配置 API Key", proc.stdout)


if __name__ == "__main__":
    unittest.main()
