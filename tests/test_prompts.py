"""Tests for prompt templates (Lead-owned)."""

from __future__ import annotations

import unittest

from pic2tex.prompts import MATH_PROMPT, STYLE_PROMPTS, build_prompt


class PromptTests(unittest.TestCase):
    def test_math_prompt_forbids_fences(self) -> None:
        prompt = build_prompt("math")
        self.assertIn("不要使用 Markdown 代码块", prompt)
        self.assertIn("LaTeX", prompt)

    def test_document_prompt(self) -> None:
        prompt = build_prompt("document")
        self.assertIn("\\documentclass", prompt)

    def test_unknown_style_falls_back(self) -> None:
        self.assertEqual(build_prompt("nope"), MATH_PROMPT)

    def test_extra_instruction_appended(self) -> None:
        prompt = build_prompt("math", "保留 \\bm 记号")
        self.assertIn("附加要求：保留 \\bm 记号", prompt)

    def test_style_registry(self) -> None:
        self.assertEqual(set(STYLE_PROMPTS), {"math", "document"})


if __name__ == "__main__":
    unittest.main()
