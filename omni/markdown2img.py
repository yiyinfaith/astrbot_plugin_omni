"""Markdown 转图片（原 astrbot_plugin_markdown2img）：LLM 用 <md></md> 包裹的内容渲染成图片。"""

from __future__ import annotations

import html
import re

from astrbot.api import logger

from .browser import SharedBrowser, block_private_requests

MD_PATTERN = re.compile(r"(<md>.*?</md>)", re.DOTALL)
# 公式先原样保护起来，避免 Markdown 把 * 当成斜体、把 \\ 和 \{ 里的反斜杠吃掉
MATH_PATTERN = re.compile(
    r"\$\$.+?\$\$|\\\[.+?\\\]|\\\(.+?\\\)|\$(?![\s$])[^\n$]+?(?<!\s)\$",
    re.DOTALL,
)

INSTRUCTION = """
当你需要发送包含复杂格式（如代码块、表格、嵌套列表等）的内容时，为了获得更好的显示效果，你可以将这部分内容渲染成一张图片。

使用规则：
1. 将你需要转换为图片的 Markdown 全文内容包裹在 `<md>` 和 `</md>` 标签之间。
2. LLM应自行判断何时使用该功能，通常用于格式复杂、纯文本难以阅读的场景。
3. 标签内的内容应为完整的、格式正确的 Markdown 文本。
4. 【重要】凡是用普通键盘文字打不出来、或纯文本显示不清楚的内容，必须放进 `<md></md>` 渲染成图片，尤其是数学表达式：分数、根号、上下标、求和、积分、极限、矩阵、方程组、希腊字母等。不要在纯文本里用 LaTeX 代码或 x^2、sqrt(x) 这类近似写法凑合。
5. 数学公式用 LaTeX 写：行内公式用 `$...$`，独立成行的公式用 `$$...$$`。

例如：
<md>
# 这是一个标题

这是一个列表:
- 列表项 1
- 列表项 2

一元二次方程 $ax^2+bx+c=0$ 的求根公式：

$$x=\\frac{-b\\pm\\sqrt{b^2-4ac}}{2a}$$

这是一个代码块:
```python
def hello_world():
    print("Hello, World!")
```
</md>
"""

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Markdown Render</title>
    <style>
        body {{
            {width_style}
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, "Noto Sans CJK SC", "Microsoft YaHei", sans-serif, "Apple Color Emoji", "Segoe UI Emoji";
            padding: 25px;
            display: inline-block;
            font-size: 16px;
            -webkit-font-smoothing: antialiased;
            -moz-osx-font-smoothing: grayscale;
            text-rendering: optimizeLegibility;
        }}
        pre {{
            background-color: #f6f8fa;
            border-radius: 6px;
            padding: 16px;
            overflow: auto;
            font-size: 85%;
            line-height: 1.45;
            white-space: pre-wrap;
            word-break: break-all;
        }}
        code {{
            font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
        }}
        table {{ border-collapse: collapse; }}
        th, td {{ border: 1px solid #d0d7de; padding: 6px 13px; }}
    </style>
    <script type="text/javascript" async
        src="https://cdnjs.cloudflare.com/ajax/libs/mathjax/2.7.7/MathJax.js?config=TeX-MML-AM_CHTML">
    </script>
    <script type="text/x-mathjax-config">
        MathJax.Hub.Config({{
            tex2jax: {{
                inlineMath: [['$','$'], ['\\\\(','\\\\)']],
                displayMath: [['$$','$$'], ['\\\\[','\\\\]']],
                processEscapes: true
            }},
            "HTML-CSS": {{ scale: 100, linebreaks: {{ automatic: true }} }},
            SVG: {{ linebreaks: {{ automatic: true }} }}
        }});
    </script>
</head>
<body>
    {content}
</body>
</html>
"""


def split_markdown(text: str) -> list[tuple[str, str]]:
    """把文本拆成 [("text", ...), ("md", ...)]，没有 <md> 标签时原样返回一个文本段。"""
    parts = []
    for part in MD_PATTERN.split(text):
        if part.startswith("<md>") and part.endswith("</md>"):
            content = part[4:-5].strip()
            if content:
                parts.append(("md", content))
        elif part.strip():
            parts.append(("text", part.strip()))
    return parts


def to_html(md_text: str) -> str:
    """Markdown 转 HTML。原始 HTML 一律转义（内容来自 LLM，可能被诱导写进 <script>）；公式原样保留给 MathJax。"""
    import mistune

    formulas: list[str] = []

    def keep(match: re.Match) -> str:
        formulas.append(match.group(0))
        return f"OMNIMATH{len(formulas) - 1}END"

    protected = MATH_PATTERN.sub(keep, md_text)
    markdown = mistune.create_markdown(escape=True, plugins=["strikethrough", "footnotes", "table"])
    body = markdown(protected)
    for i, formula in enumerate(formulas):
        body = body.replace(f"OMNIMATH{i}END", html.escape(formula, quote=False))
    return body


async def render(browser: SharedBrowser, md_text: str, output_path: str, scale: int = 2, width: int = 600) -> None:
    width_style = f"width: {width}px; box-sizing: border-box;" if width else ""
    html_text = HTML_TEMPLATE.format(content=to_html(md_text), width_style=width_style)
    instance = await browser.get()
    context = await instance.new_context(device_scale_factor=scale)
    try:
        await block_private_requests(context)
        page = await context.new_page()
        try:
            await page.set_content(html_text, wait_until="networkidle", timeout=20000)
        except Exception as exc:  # MathJax CDN 不通时也继续截图
            logger.warning(f"[Markdown 转图片] 页面加载未完成: {exc}")
        try:
            await page.evaluate("MathJax.Hub.Queue(['Typeset', MathJax.Hub])")
            await page.wait_for_function(
                "typeof MathJax.Hub.Queue.running === 'undefined' || MathJax.Hub.Queue.running === 0",
                timeout=10000,
            )
        except Exception:
            pass
        body = await page.query_selector("body")
        if not body:
            raise RuntimeError("无法找到 <body> 元素进行截图。")
        await body.screenshot(path=output_path)
    finally:
        await context.close()
