"""Tests for LaTeX post-processing (Lead-owned)."""

from __future__ import annotations

import unittest

from pic2tex.latex import (
    apply_wrapper,
    brace_balance_problems,
    clean_response,
    is_full_document,
    preview_text,
    strip_math_delimiters,
)


class CleanResponseTests(unittest.TestCase):
    def test_strips_latex_fence(self) -> None:
        raw = "```latex\n\\frac{a}{b}\n```"
        self.assertEqual(clean_response(raw), "\\frac{a}{b}")

    def test_extracts_first_block_with_prose(self) -> None:
        raw = "以下是转换结果：\n\n```tex\n\\alpha + \\beta\n```\n希望有帮助。"
        self.assertEqual(clean_response(raw), "\\alpha + \\beta")

    def test_plain_text_untouched(self) -> None:
        self.assertEqual(clean_response("  x^2 + y^2  "), "x^2 + y^2")

    def test_empty(self) -> None:
        self.assertEqual(clean_response(""), "")
        self.assertEqual(clean_response(None), "")  # type: ignore[arg-type]

    def test_crlf_normalised(self) -> None:
        self.assertEqual(clean_response("a\r\nb"), "a\nb")


class WrapTests(unittest.TestCase):
    def test_display_wrap(self) -> None:
        self.assertEqual(apply_wrapper("x^2", "display"), "\\[\nx^2\n\\]")

    def test_inline_wrap(self) -> None:
        self.assertEqual(apply_wrapper("x^2", "inline"), "$x^2$")

    def test_none_wrap(self) -> None:
        self.assertEqual(apply_wrapper("x^2", "none"), "x^2")

    def test_existing_display_delimiters_not_doubled(self) -> None:
        self.assertEqual(apply_wrapper("$$x^2$$", "display"), "\\[\nx^2\n\\]")

    def test_standalone_environment_not_wrapped(self) -> None:
        raw = "\\begin{align}\na &= b \\\\\nc &= d\n\\end{align}"
        self.assertEqual(apply_wrapper(raw, "display"), raw)
        self.assertEqual(apply_wrapper(raw, "inline"), raw)

    def test_inner_environment_is_wrapped(self) -> None:
        raw = "\\begin{aligned}\na &= b \\\\\nc &= d\n\\end{aligned}"
        self.assertEqual(apply_wrapper(raw, "display"), "\\[\n" + raw + "\n\\]")

    def test_full_document_untouched(self) -> None:
        raw = "\\documentclass{article}\n\\begin{document}\nhi\n\\end{document}"
        self.assertEqual(apply_wrapper(raw, "display"), raw)
        self.assertTrue(is_full_document(raw))

    def test_fenced_input_cleaned_before_wrap(self) -> None:
        self.assertEqual(apply_wrapper("```latex\nx\n```", "inline"), "$x$")

    def test_strip_delimiters(self) -> None:
        self.assertEqual(strip_math_delimiters("\\[a\\]"), "a")
        self.assertEqual(strip_math_delimiters("$a$"), "a")
        self.assertEqual(strip_math_delimiters("a"), "a")


class SanityCheckTests(unittest.TestCase):
    def test_balanced(self) -> None:
        self.assertEqual(brace_balance_problems("\\frac{a}{b}"), [])

    def test_unbalanced_brace(self) -> None:
        problems = brace_balance_problems("\\frac{a}{b")
        self.assertTrue(any("花括号" in problem for problem in problems))

    def test_escaped_braces_ignored(self) -> None:
        self.assertEqual(brace_balance_problems("\\{ x \\}"), [])

    def test_unclosed_environment(self) -> None:
        problems = brace_balance_problems("\\begin{align} x")
        self.assertTrue(any("align" in problem for problem in problems))

    def test_unexpected_end(self) -> None:
        problems = brace_balance_problems("x \\end{align}")
        self.assertTrue(problems)

    def test_preview_text(self) -> None:
        self.assertEqual(preview_text("a\n b   c"), "a b c")
        self.assertTrue(preview_text("x" * 100).endswith("…"))


if __name__ == "__main__":
    unittest.main()
