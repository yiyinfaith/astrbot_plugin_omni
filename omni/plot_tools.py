"""把原插件的 16 个绘图工具合并成 plot_2d / plot_3d / plot_vectors 三个入口。"""

from __future__ import annotations

import asyncio
import re

PLOT_2D_TYPES = ("function", "implicit", "polar", "parametric", "vector_field")
PLOT_3D_TYPES = ("surface", "implicit", "spherical", "parametric")
VECTOR_TYPES = ("2d", "3d", "addition", "scaling", "basis")


class SendCapture:
    """在后台线程里绘图时代替 event：先记下要发的消息，回到主线程再真正发送。"""

    def __init__(self) -> None:
        self.chains: list = []

    async def send(self, chain) -> None:
        self.chains.append(chain)


def run_in_thread(func, core, *args) -> tuple[str, list]:
    """在当前（后台）线程里跑一个绘图入口，返回 (工具返回文字, 待发送的消息)。"""
    capture = SendCapture()
    text = asyncio.run(func(core, capture, *args))
    return text, capture.chains


def split_exprs(text: str) -> list[str]:
    """多个表达式用分号分隔；没有分号时按顶层逗号（不在括号里的逗号）分隔。"""
    text = (text or "").strip()
    if ";" in text or "；" in text:
        return [p.strip() for p in re.split(r"[;；]", text) if p.strip()]
    parts, depth, buf = [], 0, ""
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf.strip())
    return parts


def to_zero_form(equation: str) -> str:
    """把 `a=b` 写成 `(a)-(b)`，已经是移项为 0 的表达式原样返回。"""
    eq = equation.strip()
    if re.search(r"(?<![<>=!])=(?!=)", eq):
        left, right = re.split(r"(?<![<>=!])=(?!=)", eq, maxsplit=1)
        right = right.strip()
        return left.strip() if right in ("", "0") else f"({left.strip()})-({right})"
    return eq


def _need(parts: list[str], count: int, example: str) -> str | None:
    if len(parts) < count:
        return f"❌ 需要 {count} 个表达式，用分号分隔，例如 \"{example}\"。"
    return None


async def plot_2d(core, event, kind, expression, x_range, y_range, t_range, title, xlabel, ylabel) -> str:
    kind = (kind or "function").strip().lower()
    common = {"title": title, "xlabel": xlabel, "ylabel": ylabel}
    parts = split_exprs(expression)
    if not parts:
        return "❌ 缺少表达式。"
    if kind == "function":
        if len(parts) > 1:
            return await core.tool_plot_multiple(
                event, expressions=";".join(parts), x_range=x_range,
                **{**common, "title": title or "函数对比"},
            )
        return await core.tool_plot_function(event, expression=parts[0], x_range=x_range, **common)
    if kind == "implicit":
        return await core.tool_plot_implicit(
            event, equation=to_zero_form(expression), x_range=x_range, y_range=y_range, **common
        )
    if kind == "polar":
        return await core.tool_plot_polar(event, expression=parts[0], theta_range=t_range, **common)
    if kind == "parametric":
        return _need(parts, 2, "cos(t);sin(t)") or await core.tool_plot_parametric(
            event, x_expression=parts[0], y_expression=parts[1], t_range=t_range, **common
        )
    if kind == "vector_field":
        return _need(parts, 2, "-y;x") or await core.tool_plot_vector_field_2d(
            event, x_expression=parts[0], y_expression=parts[1], x_range=x_range, y_range=y_range, **common
        )
    return f"❌ 不支持的类型 {kind}，可选：{', '.join(PLOT_2D_TYPES)}"


async def plot_3d(core, event, kind, expression, x_range, y_range, z_range, t_range, phi_range,
                  title, xlabel, ylabel, zlabel) -> str:
    kind = (kind or "surface").strip().lower()
    labels = {"xlabel": xlabel, "ylabel": ylabel, "zlabel": zlabel}
    parts = split_exprs(expression)
    if not parts:
        return "❌ 缺少表达式。"
    if kind == "surface":
        if len(parts) > 1:
            return await core.tool_plot_3d_multiple(
                event, expressions=";".join(parts), x_range=x_range, y_range=y_range,
                title=title or "3D 曲面对比", **labels,
            )
        return await core.tool_plot_3d_function(
            event, expression=parts[0], x_range=x_range, y_range=y_range, title=title, **labels
        )
    if kind == "implicit":
        return await core.tool_plot_implicit_3d(
            event, equation=to_zero_form(expression), x_range=x_range, y_range=y_range,
            z_range=z_range, title=title, **labels,
        )
    if kind == "spherical":
        return await core.tool_plot_3d_spherical(
            event, expression=parts[0], theta_range=t_range, phi_range=phi_range, title=title, **labels
        )
    if kind == "parametric":
        return _need(parts, 3, "cos(t);sin(t);t/5") or await core.tool_plot_3d_parametric(
            event, x_expression=parts[0], y_expression=parts[1], z_expression=parts[2],
            t_range=t_range, title=title, **labels,
        )
    return f"❌ 不支持的类型 {kind}，可选：{', '.join(PLOT_3D_TYPES)}"


async def plot_vectors(core, event, kind, vectors, scalar, title, xlabel, ylabel, zlabel) -> str:
    kind = (kind or "2d").strip().lower()
    common = {"title": title, "xlabel": xlabel, "ylabel": ylabel}
    vectors = (vectors or "").strip()
    if not vectors:
        return "❌ 缺少向量。"
    if kind == "2d":
        return await core.tool_plot_vector_2d(event, vectors=vectors, **common)
    if kind == "3d":
        return await core.tool_plot_vector_3d(event, vectors=vectors, zlabel=zlabel, **common)
    if kind == "addition":
        parts = [p.strip() for p in re.split(r"[;；]", vectors) if p.strip()]
        return _need(parts, 2, "1,2;3,1") or await core.tool_plot_vector_addition(
            event, v1=parts[0], v2=parts[1], **common
        )
    if kind == "scaling":
        try:
            k = float(scalar if scalar not in (None, "") else 2.0)
        except (TypeError, ValueError):
            return "❌ 数乘系数 scalar 必须是数字。"
        return await core.tool_plot_vector_scaling(event, vector=vectors, scalar=k, **common)
    if kind == "basis":
        return await core.tool_plot_basis_transform(event, new_basis=vectors, **common)
    return f"❌ 不支持的类型 {kind}，可选：{', '.join(VECTOR_TYPES)}"
