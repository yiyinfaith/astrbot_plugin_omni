"""Markdown 转图片（原 astrbot_plugin_markdown2img）：LLM 用 <md></md> 包裹的内容渲染成图片。"""

from __future__ import annotations

import re

from astrbot.api import logger

from .browser import SharedBrowser

MD_PATTERN = re.compile(r"(<md>.*?</md>)", re.DOTALL)

INSTRUCTION = """
当你需要发送包含复杂格式（如代码块、表格、嵌套列表等）的内容时，为了获得更好的显示效果，你可以将这部分内容渲染成一张图片。

使用规则：
1. 将你需要转换为图片的 Markdown 全文内容包裹在 `<md>` 和 `</md>` 标签之间。
2. LLM应自行判断何时使用该功能，通常用于格式复杂、纯文本难以阅读的场景。
3. 标签内的内容应为完整的、格式正确的 Markdown 文本。

例如：
<md>
# 这是一个标题

这是一个列表:
- 列表项 1
- 列表项 2

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
            tex2jax: {{ inlineMath: [['$','$']], displayMath: [['$$','$$']] }},
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


async def render(browser: SharedBrowser, md_text: str, output_path: str, scale: int = 2, width: int = 600) -> None:
    import mistune

    width_style = f"width: {width}px; box-sizing: border-box;" if width else ""
    html = HTML_TEMPLATE.format(content=mistune.html(md_text), width_style=width_style)
    instance = await browser.get()
    context = await instance.new_context(device_scale_factor=scale)
    try:
        page = await context.new_page()
        try:
            await page.set_content(html, wait_until="networkidle", timeout=20000)
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
