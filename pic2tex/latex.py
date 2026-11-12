"""Cleaning and wrapping of LaTeX returned by the model.

The model is instructed to return bare LaTeX, but small models sometimes wrap
the answer in Markdown fences or math delimiters.  These helpers normalise the
result before it is shown/copied.
"""

from __future__ import annotations

import re

_FENCE_RE = re.compile(r"```[ \t]*(?:latex|tex|LaTeX|TeX)?[ \t]*\r?\n?(.*?)```", re.DOTALL)
_FENCE_LINE_RE = re.compile(r"^\s*```[ \t]*(?:latex|tex)?[ \t]*$", re.IGNORECASE | re.MULTILINE)

_DISPLAY_DELIMS = (("$$", "$$"), ("\\[", "\\]"), ("\\begin{equation}", "\\end{equation}"),
                   ("\\begin{equation*}", "\\end{equation*}"), ("\\begin{displaymath}", "\\end{displaymath}"))
_INLINE_DELIMS = (("\\(", "\\)"), ("$", "$"))

_LEADING_NOISE_RE = re.compile(
    r"^(?:以下|如下|这是|这里是|输出|结果|LaTeX\s*(?:代码|code)?)[^\n]{0,30}[:：]\s*",
    re.IGNORECASE,
)


def _first_fenced_block(text: str) -> str | None:
    match = _FENCE_RE.search(text)
    if not match:
        return None
    body = match.group(1).strip("\n")
    return body.strip() if body.strip() else None


def clean_response(raw: str) -> str:
    """Extract the LaTeX body from a raw model answer.

    * Strips Markdown code fences (returns the first block when the answer has
      prose around it).
    * Removes a leading "以下是..." style sentence.
    * Normalises line endings and trims blank lines.
    """
    if not raw:
        return ""
    text = raw.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""

    fenced = _first_fenced_block(text)
    if fenced is not None:
        text = fenced
    else:
        text = _FENCE_LINE_RE.sub("", text).strip()

    text = _LEADING_NOISE_RE.sub("", text).strip()
    # Drop a lone language tag introduced by a malformed fence.
    text = re.sub(r"^\s*(?:latex|tex)\s*\n", "", text, flags=re.IGNORECASE)
    return text.strip("\n").strip()


def _strip_outer(text: str, pairs: tuple[tuple[str, str], ...]) -> str:
    for start, end in pairs:
        if text.startswith(start) and text.endswith(end) and len(text) >= len(start) + len(end):
            inner = text[len(start): len(text) - len(end)].strip()
            if inner:
                return inner
    return text


def strip_math_delimiters(text: str) -> str:
    """Remove one layer of outer math delimiters if present."""
    text = text.strip()
    stripped = _strip_outer(text, _DISPLAY_DELIMS)
    if stripped is not text:
        return stripped
    return _strip_outer(text, _INLINE_DELIMS)


def is_full_document(text: str) -> bool:
    return "\\begin{document}" in text or "\\documentclass" in text


def _has_outer_environment(text: str) -> bool:
    """True when the body is already a standalone math environment."""
    match = re.match(r"\s*\\begin\{([A-Za-z*]+)\}", text)
    if not match:
        return False
    env = match.group(1)
    return env in {
        # Complete display-math environments: wrapping them in \[ \] is invalid.
        "equation",
        "equation*",
        "align",
        "align*",
        "alignat",
        "alignat*",
        "flalign",
        "flalign*",
        "gather",
        "gather*",
        "multline",
        "multline*",
        "displaymath",
        "math",
        "eqnarray",
        "eqnarray*",
        # Non-math environments that must not be put inside math mode.
        "tabular",
        "tabular*",
        "tabularx",
        "longtable",
    }


def apply_wrapper(latex: str, wrapper: str) -> str:
    """Wrap a LaTeX body for display math, inline math, or no wrapper.

    Full documents and bodies that are already a complete math environment are
    never wrapped again.
    """
    body = clean_response(latex)
    if not body:
        return ""
    if is_full_document(body):
        return body
    if wrapper == "none":
        return body
    if wrapper == "inline":
        if _has_outer_environment(body):
            return body
        inner = strip_math_delimiters(body)
        return f"${inner}$"
    # default: display math
    if _has_outer_environment(body):
        return body
    inner = strip_math_delimiters(body)
    return f"\\[\n{inner}\n\\]"


def brace_balance_problems(latex: str) -> list[str]:
    """Very small sanity check for unbalanced braces/environments.

    Returns a list of human-readable warnings; an empty list means "no obvious
    problem" (not a guarantee of validity).
    """
    problems: list[str] = []
    depth = 0
    i = 0
    while i < len(latex):
        char = latex[i]
        if char == "\\":
            i += 2  # skip escaped char (also skips \{ and \})
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                problems.append("出现了多余的右花括号 }")
                depth = 0
        i += 1
    if depth:
        problems.append(f"花括号不平衡（缺少 {depth} 个 }}）")

    begin_stack: list[str] = []
    for match in re.finditer(r"\\(begin|end)\{([A-Za-z*]+)\}", latex):
        kind, env = match.group(1), match.group(2)
        if kind == "begin":
            begin_stack.append(env)
        elif begin_stack and begin_stack[-1] == env:
            begin_stack.pop()
        elif env in begin_stack:
            problems.append(f"环境嵌套顺序可疑：\\end{{{env}}}")
            begin_stack.remove(env)
        else:
            problems.append(f"缺少对应的 \\begin{{{env}}}")
    for env in begin_stack:
        problems.append(f"缺少 \\end{{{env}}}")
    return problems


def preview_text(latex: str, limit: int = 70) -> str:
    """Single-line preview used by the history list."""
    flat = re.sub(r"\s+", " ", latex).strip()
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"
