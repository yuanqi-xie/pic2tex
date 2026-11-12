"""Prompt templates for converting images to LaTeX with a vision model."""

from __future__ import annotations

MATH_PROMPT = """你是一名精通 LaTeX 与数学排版的专家。请把你看到的图片中的数学内容，完整、准确地转录为 LaTeX 代码。

必须遵守：
1. 只输出 LaTeX 代码本身。不要使用 Markdown 代码块（不要出现 ```），不要输出任何解释、前言、后记或“以下是”等字样。
2. 不要添加 $$、$、\\( \\)、\\[ \\] 等数学定界符，只输出公式主体。
3. 严格保留原图的符号、上下标、分式、根式、求和/积分上下限、希腊字母、重音、运算符、括号与多行对齐结构。
4. 多行公式使用 aligned、cases、matrix、array 等环境；假定已经加载 amsmath、amssymb、bm，不要写 \\usepackage。
5. 区分手写体与印刷体，都要识别；模糊字符要结合上下文推断，不要臆造，也不要用占位符。
6. 字体语义要还原：例如 \\mathbb{{R}}、\\mathcal{{L}}、\\mathbf{{x}}、\\mathrm{{d}}、\\operatorname{{softmax}}。
7. 如果图中含有少量相邻文字说明，可用 \\text{{...}} 原样保留；如果图片主要内容不是公式，也要输出最接近的 LaTeX 表示。
8. 输出必须是可直接粘贴进数学环境的、语法正确的 LaTeX。"""

DOCUMENT_PROMPT = """你是一名精通 LaTeX 的排版专家。请把图片中的整页内容还原成一份完整、可以直接编译的 LaTeX 文档。

必须遵守：
1. 输出完整文档：\\documentclass[11pt]{{article}}、必要的宏包（如 amsmath、amssymb、graphicx、booktabs、geometry、hyperref 等）、\\begin{{document}} … \\end{{document}}。
2. 只输出 LaTeX 代码本身，不要使用 Markdown 代码块（不要出现 ```），不要任何解释或前言后记。
3. 尽量还原版面结构：标题、作者、章节、段落、列表、公式（行间用 equation/align，行内用 $...$）、表格（tabular/booktabs）、图题等。
4. 逐字保留图片中的文字内容（中英文均按原文），不要总结、改写或翻译。
5. 不确定的公式或字符要结合上下文给出最合理的 LaTeX 表示，不要留下占位符。
6. 输出必须语法正确、可直接编译。"""

STYLE_PROMPTS = {"math": MATH_PROMPT, "document": DOCUMENT_PROMPT}


def build_prompt(style: str = "math", extra: str = "") -> str:
    """Return the prompt for *style*, optionally with a user instruction appended."""
    base = STYLE_PROMPTS.get(style, MATH_PROMPT)
    extra = (extra or "").strip()
    if not extra:
        return base
    return f"{base}\n\n附加要求：{extra}"
