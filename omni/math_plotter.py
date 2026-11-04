"""数学函数绘图（原 astrbot_plugin_math_plotter），绘图逻辑与原插件一致。"""

from __future__ import annotations

import os
import re
import uuid

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from sympy import SympifyError, latex, lambdify, symbols, sympify  # noqa: E402
from sympy.abc import x as sym_x  # noqa: E402

import astrbot.api.message_components as Comp  # noqa: E402
from astrbot.api import logger  # noqa: E402
from astrbot.api.event import AstrMessageEvent, MessageEventResult  # noqa: E402
from astrbot.core.message.message_event_result import MessageChain  # noqa: E402

CJK_CANDIDATES = [
    "Noto Sans CJK SC", "Noto Sans SC", "Noto Sans CJK TC",
    "Noto Serif CJK SC", "WenQuanYi Micro Hei", "WenQuanYi Zen Hei",
    "Source Han Sans SC", "AR PL UMing CN", "AR PL UKai CN",
    "Droid Sans Fallback", "SimHei", "Microsoft YaHei",
]
_cjk_font: str | None = None
_font_checked = False


def ensure_cjk_font() -> str | None:
    """检测可用的中文字体；字体缓存里没有时才重新扫描系统字体（只做一次）。"""
    global _cjk_font, _font_checked
    if _font_checked:
        return _cjk_font
    from matplotlib import font_manager as fm

    def pick():
        available = {f.name for f in fm.fontManager.ttflist}
        return next((name for name in CJK_CANDIDATES if name in available), None)

    _cjk_font = pick()
    if _cjk_font is None:
        fm.fontManager = fm.FontManager()
        _cjk_font = pick()
    _font_checked = True
    plt.rcParams["font.sans-serif"] = ([_cjk_font] if _cjk_font else []) + ["DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    if not _cjk_font:
        logger.warning("[数学绘图] 未检测到中文字体，图中的中文可能显示为方框。可安装 fonts-noto-cjk。")
    return _cjk_font


class MathPlotterCore:
    """原 MathPlotter 的全部绘图逻辑，不含 AstrBot 装饰器。"""

    def __init__(self, config_getter, plots_dir: str):
        self._config_getter = config_getter
        self.plots_dir = plots_dir
        os.makedirs(plots_dir, exist_ok=True)
        ensure_cjk_font()

    # ── helpers ──

    def _get_config(self, key: str, default=None):
        value = self._config_getter(key)
        return default if value is None else value

    @staticmethod
    def _preprocess_expr(raw: str) -> str:
        """预处理：^ → **，max/min → Max/Min，|x| → abs(x) 等友好转换。"""
        s = raw.strip()
        s = re.sub(r"(\d+|[a-zA-Z])\^(\d+)", r"\1**\2", s)
        s = re.sub(r"(\d+|[a-zA-Z])\^\((.+?)\)", r"\1**(\2)", s)
        # Python max/min → SymPy Max/Min
        s = re.sub(r"\bmax\s*\(", "Max(", s)
        s = re.sub(r"\bmin\s*\(", "Min(", s)
        return s

    @staticmethod
    def _parse_expr(expression: str, extra_symbols: tuple = ()):
        _s = __import__("sympy")
        locals_dict = {
            "x": sym_x,
            "sin": _s.sin, "cos": _s.cos, "tan": _s.tan,
            "exp": _s.exp, "log": _s.log, "sqrt": _s.sqrt,
            "abs": _s.Abs, "pi": _s.pi, "E": _s.E, "e": _s.E,
            "Max": _s.Max, "Min": _s.Min,
            "Heaviside": _s.Heaviside, "sign": _s.sign,
            "Piecewise": _s.Piecewise,
        }
        for sym_name in extra_symbols:
            locals_dict[sym_name] = symbols(sym_name)
        return sympify(expression, locals=locals_dict)

    @staticmethod
    def _expr_to_func(expr, variable=sym_x):
        return lambdify(variable, expr, "numpy")

    def _make_figure(self):
        dpi = self._get_config("plot_dpi", 120)
        fig, ax = plt.subplots(figsize=(10, 6), dpi=dpi, constrained_layout=True)
        return fig, ax

    def _style_axes(self, ax):
        alpha = self._get_config("grid_alpha", 0.3)
        ax.grid(True, alpha=alpha, linestyle="--")
        ax.axhline(y=0, color="black", linewidth=0.8)
        ax.axvline(x=0, color="black", linewidth=0.8)
        ax.set_xlabel("x", fontsize=12)
        ax.set_ylabel("y", fontsize=12)

    def _save_and_close(self, fig, tight: bool = True) -> str:
        filename = f"plot_{uuid.uuid4().hex[:8]}.png"
        filepath = os.path.join(self.plots_dir, filename)
        dpi = self._get_config("plot_dpi", 120)
        kwargs = dict(dpi=dpi, facecolor="white")
        if tight:
            kwargs["bbox_inches"] = "tight"
        fig.savefig(filepath, **kwargs)
        plt.close(fig)
        return filepath

    @staticmethod
    def _safe_numpy(func, x_vals):
        try:
            y_vals = func(x_vals)
            if isinstance(y_vals, (int, float, complex)):
                y_vals = np.full_like(x_vals, y_vals, dtype=float)
            y_vals = np.array(y_vals, dtype=float)
            mask = np.isfinite(y_vals)
            return x_vals[mask], y_vals[mask]
        except Exception:
            return np.array([]), np.array([])

    def _parse_range(self, range_str: str):
        try:
            parts = range_str.split(",")
            return float(parts[0].strip()), float(parts[1].strip())
        except Exception:
            default = self._get_config("default_x_range", "-10,10")
            return self._parse_range(default)

    # ── 公共发送逻辑 ──
    async def _send_result(self, event, description: str, filepath: str):
        """发送图文给用户。兼容 AstrBot v4.26+ ContextWrapper 和旧版 AstrMessageEvent。"""
        # 解包 ContextWrapper（v4.26+），提取真实的 AstrMessageEvent
        real_event = getattr(event, "event", None) or event
        if not hasattr(real_event, "send"):
            # 更深层解包：某些版本中 event 封装在 context 里
            ctx = getattr(event, "context", None)
            if ctx and hasattr(ctx, "event"):
                real_event = ctx.event
        await real_event.send(MessageChain(
            chain=[Comp.Plain(description), Comp.Image.fromFileSystem(filepath)],
            type="tool_direct_result",
        ))

    # ── 文本清洗：将 Unicode 上下标转为纯文本，避免 matplotlib 字体缺字 ──

    @staticmethod
    def _sanitize_math_text(s: str) -> str:
        """将 Unicode 数学下标/上标替换为普通 ASCII 字符，确保 matplotlib 正常渲染。"""
        if not s:
            return s
        trans = str.maketrans({
            "₀": "0", "₁": "1", "₂": "2", "₃": "3", "₄": "4",
            "₅": "5", "₆": "6", "₇": "7", "₈": "8", "₉": "9",
            "₊": "+", "₋": "-", "₌": "=", "₍": "(", "₎": ")",
            "⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4",
            "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9",
            "⁺": "+", "⁻": "-", "⁼": "=", "⁽": "(", "⁾": ")",
        })
        return s.translate(trans)

    # ── 向量绘图辅助方法 ──

    @staticmethod
    def _parse_point(s: str):
        """解析 "(x,y)" 或 "x,y" 格式的二维坐标，返回 (x, y)。"""
        s = s.strip().lstrip("(").rstrip(")").strip()
        parts = s.split(",")
        if len(parts) != 2:
            raise ValueError(f"无法解析坐标: {s}")
        return float(parts[0].strip()), float(parts[1].strip())

    @staticmethod
    def _parse_point_3d(s: str):
        """解析 "(x,y,z)" 或 "x,y,z" 格式的三维坐标，返回 (x, y, z)。"""
        s = s.strip().lstrip("(").rstrip(")").strip()
        parts = s.split(",")
        if len(parts) != 3:
            raise ValueError(f"无法解析三维坐标: {s}")
        return float(parts[0].strip()), float(parts[1].strip()), float(parts[2].strip())

    @staticmethod
    def _parse_vector_def(def_str: str):
        """解析单个向量定义字符串。

        支持格式：
            "x,y"                     → 从原点出发
            "(x1,y1)->(x2,y2)"       → 指定起终点
            "x1,y1->x2,y2"           → 同上（无括号）
        可附加 :颜色 :标签，如 "0,0->3,4:red:v1"
        返回 dict: {start, end, color, label}
        """
        s = def_str.strip()
        color = "#2196F3"
        label = ""
        # 按 ":" 分割（颜色和标签可选）
        if ":" in s:
            parts = s.split(":")
            s = parts[0].strip()
            extras = [p.strip() for p in parts[1:]]
            # 颜色：英文名或 #hex
            _named = {
                "红": "#F44336", "red": "#F44336",
                "蓝": "#2196F3", "blue": "#2196F3",
                "绿": "#4CAF50", "green": "#4CAF50",
                "橙": "#FF9800", "orange": "#FF9800",
                "紫": "#9C27B0", "purple": "#9C27B0",
                "灰": "#9E9E9E", "gray": "#9E9E9E",
                "黑": "#000000", "black": "#000000",
            }
            for ext in extras:
                if ext.lower() in _named:
                    color = _named[ext.lower()]
                elif ext.startswith("#") or ext in (
                    "red", "blue", "green", "orange", "purple", "gray", "black",
                    "cyan", "magenta", "yellow", "brown", "pink", "lime",
                ):
                    color = ext
                else:
                    label = ext  # 非颜色的视为标签
        # 解析坐标
        if "->" in s:
            start_str, end_str = s.split("->")
        else:
            start_str, end_str = "0,0", s
        start = MathPlotterCore._parse_point(start_str)
        end = MathPlotterCore._parse_point(end_str)
        return {"start": start, "end": end, "color": color,
                "label": MathPlotterCore._sanitize_math_text(label)}

    @staticmethod
    def _parse_vector_def_3d(def_str: str):
        """解析三维向量定义字符串，格式类似二维但坐标为 (x,y,z)。"""
        s = def_str.strip()
        color = "#2196F3"
        label = ""
        if ":" in s:
            parts = s.split(":")
            s = parts[0].strip()
            extras = [p.strip() for p in parts[1:]]
            _named = {
                "红": "#F44336", "red": "#F44336",
                "蓝": "#2196F3", "blue": "#2196F3",
                "绿": "#4CAF50", "green": "#4CAF50",
                "橙": "#FF9800", "orange": "#FF9800",
                "紫": "#9C27B0", "purple": "#9C27B0",
                "灰": "#9E9E9E", "gray": "#9E9E9E",
            }
            for ext in extras:
                if ext.lower() in _named:
                    color = _named[ext.lower()]
                elif ext.startswith("#") or ext in (
                    "red", "blue", "green", "orange", "purple", "gray", "black",
                    "cyan", "magenta", "yellow",
                ):
                    color = ext
                else:
                    label = ext
        if "->" in s:
            start_str, end_str = s.split("->")
        else:
            start_str, end_str = "0,0,0", s
        start = MathPlotterCore._parse_point_3d(start_str)
        end = MathPlotterCore._parse_point_3d(end_str)
        return {"start": start, "end": end, "color": color,
                "label": MathPlotterCore._sanitize_math_text(label)}

    def _draw_2d_arrow(self, ax, start, end, color: str, lw: float,
                        label: str = "", label_offset=(0, 0)):
        """在 ax 上绘制一个带实心三角箭头的向量。

        Args:
            ax: matplotlib Axes
            start: (x, y) 起点
            end: (x, y) 终点
            color: 颜色
            lw: 线宽
            label: 标签文字
            label_offset: 标签相对于终点的 (dx, dy) 偏移（数据坐标）
        """
        from matplotlib.patches import FancyArrowPatch
        arrow = FancyArrowPatch(
            start, end,
            arrowstyle="-|>", mutation_scale=18,
            color=color, linewidth=lw, zorder=4,
        )
        ax.add_patch(arrow)
        if label:
            # 默认偏移：沿向量方向外推一小段
            dx = end[0] - start[0]
            dy = end[1] - start[1]
            vec_len = np.sqrt(dx ** 2 + dy ** 2)
            if vec_len > 1e-6:
                # 标签放在终点外侧 8% 处，再加用户指定的偏移
                default_off_x = dx / vec_len * 0.3
                default_off_y = dy / vec_len * 0.3
            else:
                default_off_x, default_off_y = 0.3, 0.3
            lx = end[0] + default_off_x + label_offset[0]
            ly = end[1] + default_off_y + label_offset[1]
            ax.annotate(
                label, xy=end, xytext=(lx, ly),
                fontsize=11, color=color,
                bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="none", alpha=0.7),
                zorder=5,
            )

    def _render_3d_vectors(self, vector_list: list, title: str, dpi: int,
                                   xlabel: str = "x", ylabel: str = "y", zlabel: str = "z") -> str:
        """用 plotly 渲染三维向量图。每个向量用线段 + 末端锥体箭头表示。

        Args:
            vector_list: list of dict，每个含 start, end, color, label
            title: 图表标题
            dpi: 分辨率
            xlabel, ylabel, zlabel: 轴标签
        Returns: 文件路径
        """
        fig = plt.figure(figsize=(12, 9), dpi=dpi, facecolor="white")
        ax = fig.add_subplot(111, projection="3d")
        ax.computed_zorder = False

        all_pts = []
        for v in vector_list:
            sx, sy, sz = v["start"]
            ex, ey, ez = v["end"]
            dx, dy, dz = ex - sx, ey - sy, ez - sz
            all_pts.extend([sx, ex, sy, ey, sz, ez])
            # quiver 画箭头（从起点指向终点）
            ax.quiver(sx, sy, sz, dx, dy, dz, color=v["color"],
                      arrow_length_ratio=0.15, linewidth=2,
                      label=v.get("label") or None)
            # 终点标记球
            ax.scatter([ex], [ey], [ez], color=v["color"], s=20, alpha=0.8)

        # 自动确定坐标轴范围
        if all_pts:
            xs, ys, zs = all_pts[::3], all_pts[1::3], all_pts[2::3]
            rng = max(max(xs)-min(xs), max(ys)-min(ys), max(zs)-min(zs), 4) * 0.2 + 1
            ax.set_xlim(min(xs)-rng, max(xs)+rng)
            ax.set_ylim(min(ys)-rng, max(ys)+rng)
            ax.set_zlim(min(zs)-rng, max(zs)+rng)

        ax.set_xlabel(xlabel, fontsize=10)
        ax.set_ylabel(ylabel, fontsize=10)
        ax.set_zlabel(zlabel, fontsize=10)
        ax.set_title(title, fontsize=14)
        if any(v.get("label") for v in vector_list):
            ax.legend(fontsize=9)

        filename = f"plot_{uuid.uuid4().hex[:8]}.png"
        filepath = os.path.join(self.plots_dir, filename)
        fig.savefig(filepath, dpi=dpi, facecolor="white", bbox_inches="tight")
        plt.close(fig)
        return filepath

    # ═══════════════════════════════════════════════════
    #  LLM 工具 #1：一元函数 y = f(x)
    # ═══════════════════════════════════════════════════

    async def tool_plot_function(
        self, event: AstrMessageEvent,
        expression: str, x_range: str = "", title: str = "",
        xlabel: str = "", ylabel: str = "",
    ) -> MessageEventResult:
        """绘制一元函数 y=f(x) 的图像。支持 sin、cos、tan、exp、log、sqrt、abs、pi 等。

        Args:
            expression(string): 函数表达式。例如 sin(x)、x**2、exp(-x**2)
            x_range(string): x 轴范围 "min,max"，默认 "-10,10"
            title(string): 图表标题，留空自动生成
            xlabel(string): x 轴标签，留空默认 "x"
            ylabel(string): y 轴标签，留空默认 "y"
        """
        try:
            raw = self._preprocess_expr(expression)
            expr = self._parse_expr(raw)
            f = self._expr_to_func(expr)

            if x_range:
                x_min, x_max = self._parse_range(x_range)
            else:
                x_min, x_max = self._parse_range(self._get_config("default_x_range", "-10,10"))

            x_vals = np.linspace(x_min, x_max, 2000)
            x_vals, y_vals = self._safe_numpy(f, x_vals)
            if len(x_vals) == 0:
                return f"❌ 无法计算表达式「{expression}」的函数值，请检查定义域。"

            lw = self._get_config("line_width", 2.0)
            fig, ax = self._make_figure()
            ax.plot(x_vals, y_vals, linewidth=lw, color="#2196F3", label=f"$y = {latex(expr)}$")
            self._style_axes(ax)
            if xlabel: ax.set_xlabel(xlabel)
            if ylabel: ax.set_ylabel(ylabel)
            ax.legend(fontsize=11)
            if title:
                ax.set_title(title, fontsize=14)
            else:
                ax.set_title(f"$y = {latex(expr)}$", fontsize=14)

            filepath = self._save_and_close(fig)
            description = f"📈 已绘制函数 $y = {latex(expr)}$ 的图像，x 范围 [{x_min}, {x_max}]。"
            await self._send_result(event, description, filepath)
            return description
        except SympifyError as e:
            return f"❌ 表达式解析错误：「{expression}」无法识别。详情: {e}"
        except Exception as e:
            logger.exception(f"绘图失败: {e}")
            return f"❌ 绘制「{expression}」时出错: {e}"

    # ═══════════════════════════════════════════════════
    #  LLM 工具 #2：多函数对比
    # ═══════════════════════════════════════════════════

    async def tool_plot_multiple(
        self, event: AstrMessageEvent,
        expressions: str, x_range: str = "", title: str = "函数对比",
        xlabel: str = "", ylabel: str = "",
    ) -> MessageEventResult:
        """在同一坐标系中绘制多个函数图像进行对比。

        Args:
            expressions(string): 逗号分隔的表达式。例如 "sin(x),cos(x),x**2"
            x_range(string): x 轴范围 "min,max"，默认 "-10,10"
            title(string): 图表标题
            xlabel(string): x 轴标签，留空默认 "x"
            ylabel(string): y 轴标签，留空默认 "y"
        """
        try:
            expr_list = [e.strip() for e in expressions.split(";" if ";" in expressions else ",") if e.strip()]
            if len(expr_list) < 2:
                return "❌ 请提供至少 2 个逗号分隔的表达式，例如：sin(x),cos(x)"
            if len(expr_list) > 6:
                return "❌ 最多支持 6 个表达式同时对比。"

            if x_range:
                x_min, x_max = self._parse_range(x_range)
            else:
                x_min, x_max = self._parse_range(self._get_config("default_x_range", "-10,10"))

            colors = ["#2196F3", "#F44336", "#4CAF50", "#FF9800", "#9C27B0", "#00BCD4"]
            lw = self._get_config("line_width", 2.0)
            fig, ax = self._make_figure()
            x_vals = np.linspace(x_min, x_max, 2000)

            for i, raw_expr in enumerate(expr_list):
                processed = self._preprocess_expr(raw_expr)
                expr = self._parse_expr(processed)
                f = self._expr_to_func(expr)
                xs, ys = self._safe_numpy(f, x_vals)
                if len(xs) > 0:
                    ax.plot(xs, ys, linewidth=lw, color=colors[i % len(colors)],
                            label=f"$y = {latex(expr)}$")

            self._style_axes(ax)
            if xlabel: ax.set_xlabel(xlabel)
            if ylabel: ax.set_ylabel(ylabel)
            ax.legend(fontsize=11)
            ax.set_title(title, fontsize=14)

            filepath = self._save_and_close(fig)
            expr_tex = ", ".join([f"$y={latex(self._parse_expr(self._preprocess_expr(e.strip())))}" for e in expr_list])
            description = f"📈 已绘制函数对比图像：{expr_tex}，x 范围 [{x_min}, {x_max}]。"
            await self._send_result(event, description, filepath)
            return description
        except SympifyError as e:
            return f"❌ 表达式解析错误。详情: {e}"
        except Exception as e:
            logger.exception(f"多函数绘图失败: {e}")
            return f"❌ 绘图时出错: {e}"

    # ═══════════════════════════════════════════════════
    #  LLM 工具 #3：隐式方程
    # ═══════════════════════════════════════════════════

    async def tool_plot_implicit(
        self, event: AstrMessageEvent,
        equation: str, x_range: str = "", y_range: str = "", title: str = "",
        xlabel: str = "", ylabel: str = "",
    ) -> MessageEventResult:
        """绘制隐式方程 F(x,y)=0 的图像。将方程移项为 0 的形式传入。
        例如 x²+y²=1 → "x**2+y**2-1"，xy=1 → "x*y-1"。

        Args:
            equation(string): 移项为 0 的方程表达式
            x_range(string): x 轴范围，默认 "-5,5"
            y_range(string): y 轴范围，默认同 x_range
            title(string): 图表标题
            xlabel(string): x 轴标签，留空默认 "x"
            ylabel(string): y 轴标签，留空默认 "y"
        """
        try:
            raw = self._preprocess_expr(equation)
            y_sym = symbols("y")
            locals_dict = {
                "x": sym_x, "y": y_sym,
                "sin": __import__("sympy").sin, "cos": __import__("sympy").cos,
                "tan": __import__("sympy").tan, "exp": __import__("sympy").exp,
                "log": __import__("sympy").log, "sqrt": __import__("sympy").sqrt,
                "abs": __import__("sympy").Abs, "pi": __import__("sympy").pi,
                "E": __import__("sympy").E,
            }
            expr = sympify(raw, locals=locals_dict)
            f = lambdify((sym_x, y_sym), expr, "numpy")

            if x_range:
                x_min, x_max = self._parse_range(x_range)
            else:
                x_min, x_max = -5.0, 5.0
            if y_range:
                y_min, y_max = self._parse_range(y_range)
            else:
                y_min, y_max = x_min, x_max

            xs = np.linspace(x_min, x_max, 400)
            ys = np.linspace(y_min, y_max, 400)
            X, Y = np.meshgrid(xs, ys)
            Z = f(X, Y)

            lw = self._get_config("line_width", 2.0)
            fig, ax = self._make_figure()
            ax.contour(X, Y, Z, levels=[0], colors="#2196F3", linewidths=lw)
            ax.contour(X, Y, Z, levels=10, colors="gray", linewidths=0.3, alpha=0.5)
            self._style_axes(ax)
            if title:
                ax.set_title(title, fontsize=14)
            else:
                ax.set_title(f"${latex(expr)} = 0$", fontsize=14)

            filepath = self._save_and_close(fig)
            description = f"📈 已绘制隐式方程 ${latex(expr)} = 0$ 的图像，x 范围 [{x_min}, {x_max}]，y 范围 [{y_min}, {y_max}]。"
            await self._send_result(event, description, filepath)
            return description
        except SympifyError as e:
            return f"❌ 方程表达式解析错误。详情: {e}"
        except Exception as e:
            logger.exception(f"隐式方程绘图失败: {e}")
            return f"❌ 绘制隐式方程时出错: {e}"

    # ═══════════════════════════════════════════════════
    #  手动 3D 投影渲染（无需 Axes3D，避免 BboxTransformTo 错误）
    # ═══════════════════════════════════════════════════

    def _render_3d_surface(
        self, X: np.ndarray, Y: np.ndarray, Z: np.ndarray,
        cmap_name: str = "viridis",
        elev: float = 25, azim: float = -60,
        dpi: int = 150,
        title: str = "",
        xlabel: str = "x", ylabel: str = "y", zlabel: str = "z",
    ) -> str:
        """用 matplotlib 原生 3D 渲染曲面，无需 Chrome/WebGL，Docker 可用。"""
        fig = plt.figure(figsize=(12, 9), dpi=dpi, facecolor="white")
        ax = fig.add_subplot(111, projection="3d")
        ax.computed_zorder = False

        # 渲染曲面
        surf = ax.plot_surface(
            X, Y, Z, cmap=cmap_name, alpha=float(self._get_config("plot_3d_alpha", 0.88)),
            linewidth=0, antialiased=True,
        )
        # 底面投影等高线
        if self._get_config("plot_3d_contour_offset", True):
            ax.contour(X, Y, Z, zdir="z", offset=np.nanmin(Z), levels=10,
                       colors="gray", linewidths=0.3, alpha=0.4)
        fig.colorbar(surf, ax=ax, shrink=0.5, aspect=12, label=zlabel)

        ax.set_xlabel(xlabel, fontsize=10)
        ax.set_ylabel(ylabel, fontsize=10)
        ax.set_zlabel(zlabel, fontsize=10)
        ax.set_title(title, fontsize=14)
        ax.view_init(elev=elev, azim=azim)

        filename = f"plot_{uuid.uuid4().hex[:8]}.png"
        filepath = os.path.join(self.plots_dir, filename)
        fig.savefig(filepath, dpi=dpi, facecolor="white", bbox_inches="tight")
        plt.close(fig)
        return filepath

    async def tool_plot_3d_function(
        self, event: AstrMessageEvent,
        expression: str, x_range: str = "", y_range: str = "", title: str = "",
        xlabel: str = "", ylabel: str = "", zlabel: str = "",
    ) -> MessageEventResult:
        """绘制三维函数 z=f(x,y) 的曲面图像。变量为 x 和 y。

        Args:
            expression(string): 二元函数表达式。如 sin(sqrt(x**2+y**2))、x**2+y**2
            x_range(string): x 轴范围，默认 "-5,5"
            y_range(string): y 轴范围，默认同 x_range
            title(string): 图表标题
            xlabel(string): x 轴标签，留空默认 "x"
            ylabel(string): y 轴标签，留空默认 "y"
            zlabel(string): z 轴标签，留空默认 "z"
        """
        try:
            raw = self._preprocess_expr(expression)
            y_sym = symbols("y")
            expr = self._parse_expr(raw, extra_symbols=("y",))
            f = lambdify((sym_x, y_sym), expr, "numpy")

            if x_range:
                x_min, x_max = self._parse_range(x_range)
            else:
                x_min, x_max = -5.0, 5.0
            if y_range:
                y_min, y_max = self._parse_range(y_range)
            else:
                y_min, y_max = x_min, x_max

            grid_density = self._get_config("plot_3d_grid_density", 200)
            xs = np.linspace(x_min, x_max, grid_density)
            ys = np.linspace(y_min, y_max, grid_density)
            X, Y = np.meshgrid(xs, ys)

            try:
                Z = f(X, Y)
                if isinstance(Z, (int, float, complex)):
                    Z = np.full_like(X, Z, dtype=float)
                Z = np.array(Z, dtype=float)
                Z[~np.isfinite(Z)] = np.nan
            except Exception:
                return f"❌ 无法计算表达式「{expression}」的函数值，请检查定义域。"

            if np.all(np.isnan(Z)):
                return f"❌ 表达式「{expression}」在整个区域内均无有效值，请检查定义域。"

            latex_expr = latex(expr)
            dpi = self._get_config("plot_dpi", 120)
            cmap = self._get_config("plot_3d_cmap", "viridis")

            # ── Plotly 真 3D 渲染 ──
            plot_title = title if title else f"$z = {latex_expr}$"
            filepath = self._render_3d_surface(
                X, Y, Z,
                cmap_name=cmap,
                elev=self._get_config("plot_3d_elev", 25),
                azim=self._get_config("plot_3d_azim", -60),
                dpi=dpi,
                title=plot_title,
                xlabel=xlabel or "x",
                ylabel=ylabel or "y",
                zlabel=zlabel or "z",
            )
            description = (f"📈 已绘制三维曲面 $z = {latex_expr}$ 的图像，"
                           f"x 范围 [{x_min}, {x_max}]，y 范围 [{y_min}, {y_max}]。")

            await self._send_result(event, description, filepath)
            return description
        except SympifyError as e:
            return f"❌ 表达式解析错误：「{expression}」无法识别。详情: {e}"
        except Exception as e:
            logger.exception(f"3D 绘图失败: {e}")
            return f"❌ 绘制三维函数「{expression}」时出错: {e}"

    # ═══════════════════════════════════════════════════
    #  LLM 工具 #5：极坐标 r = f(θ)
    # ═══════════════════════════════════════════════════

    async def tool_plot_polar(
        self, event: AstrMessageEvent,
        expression: str, theta_range: str = "", title: str = "",
        xlabel: str = "", ylabel: str = "",
    ) -> MessageEventResult:
        """绘制极坐标方程 r=f(θ) 的图像。变量为 theta。

        Args:
            expression(string): 极坐标表达式。例如 sin(3*theta)、1+cos(theta)、theta
            theta_range(string): θ 范围 "min,max"，默认 "0,6.2832"
            title(string): 图表标题
            xlabel(string): x 轴标签，留空默认 "x"
            ylabel(string): y 轴标签，留空默认 "y"
        """
        try:
            theta_sym = symbols("theta")
            raw = self._preprocess_expr(expression)
            expr = self._parse_expr(raw, extra_symbols=("theta",))
            f = lambdify(theta_sym, expr, "numpy")

            if theta_range:
                t_min, t_max = self._parse_range(theta_range)
            else:
                t_min, t_max = 0.0, 2 * np.pi

            theta_vals = np.linspace(t_min, t_max, 2000)
            try:
                r_vals = f(theta_vals)
                r_vals = np.array(r_vals, dtype=float)
                r_vals[~np.isfinite(r_vals)] = np.nan
            except Exception:
                return f"❌ 无法计算表达式「{expression}」的函数值，请检查定义域。"

            mask = np.isfinite(r_vals)
            theta_vals, r_vals = theta_vals[mask], r_vals[mask]
            if len(theta_vals) == 0:
                return f"❌ 表达式「{expression}」在指定范围内无有效值。"

            dpi = self._get_config("plot_dpi", 120)
            lw = self._get_config("line_width", 2.0)
            fig = plt.figure(figsize=(10, 10), dpi=dpi)
            ax = fig.add_subplot(111, projection="polar")
            ax.plot(theta_vals, r_vals, linewidth=lw, color="#E91E63", label=f"$r = {latex(expr)}$")
            ax.legend(fontsize=11, loc="upper right")
            ax.grid(True, alpha=0.3, linestyle="--")
            if xlabel: ax.set_xlabel(xlabel)
            if title:
                ax.set_title(title, fontsize=14, pad=15)
            else:
                ax.set_title(f"$r = {latex(expr)}$", fontsize=14, pad=15)

            filepath = self._save_and_close(fig, tight=False)
            description = f"📈 已绘制极坐标 $r = {latex(expr)}$ 的图像，θ 范围 [{t_min:.2f}, {t_max:.2f}]。"
            await self._send_result(event, description, filepath)
            return description
        except SympifyError as e:
            return f"❌ 表达式解析错误：「{expression}」无法识别。详情: {e}"
        except Exception as e:
            logger.exception(f"极坐标绘图失败: {e}")
            return f"❌ 绘制极坐标「{expression}」时出错: {e}"

    # ═══════════════════════════════════════════════════
    #  LLM 工具 #6：参数方程 x=f(t), y=g(t)
    # ═══════════════════════════════════════════════════

    async def tool_plot_parametric(
        self, event: AstrMessageEvent,
        x_expression: str, y_expression: str, t_range: str = "", title: str = "",
        xlabel: str = "", ylabel: str = "",
    ) -> MessageEventResult:
        """绘制参数方程 x=f(t), y=g(t) 的图像。变量为 t。

        Args:
            x_expression(string): x(t) 表达式。例如 cos(t)、t-sin(t)
            y_expression(string): y(t) 表达式。例如 sin(t)、1-cos(t)
            t_range(string): t 范围 "min,max"，默认 "0,6.2832"
            title(string): 图表标题
            xlabel(string): x 轴标签，留空默认 "x"
            ylabel(string): y 轴标签，留空默认 "y"
        """
        try:
            t_sym = symbols("t")
            raw_x = self._preprocess_expr(x_expression)
            raw_y = self._preprocess_expr(y_expression)
            expr_x = self._parse_expr(raw_x, extra_symbols=("t",))
            expr_y = self._parse_expr(raw_y, extra_symbols=("t",))
            fx = lambdify(t_sym, expr_x, "numpy")
            fy = lambdify(t_sym, expr_y, "numpy")

            if t_range:
                t_min, t_max = self._parse_range(t_range)
            else:
                t_min, t_max = 0.0, 2 * np.pi

            t_vals = np.linspace(t_min, t_max, 3000)
            try:
                x_vals = np.array(fx(t_vals), dtype=float)
                y_vals = np.array(fy(t_vals), dtype=float)
            except Exception:
                return "❌ 无法计算表达式的函数值，请检查定义域。"

            mask = np.isfinite(x_vals) & np.isfinite(y_vals)
            x_vals, y_vals = x_vals[mask], y_vals[mask]
            if len(x_vals) == 0:
                return "❌ 参数方程在指定范围内无有效值。"

            lw = self._get_config("line_width", 2.0)
            fig, ax = self._make_figure()
            ax.plot(x_vals, y_vals, linewidth=lw, color="#9C27B0",
                    label=f"$(x = {latex(expr_x)},\\; y = {latex(expr_y)})$")
            self._style_axes(ax)
            if xlabel: ax.set_xlabel(xlabel)
            if ylabel: ax.set_ylabel(ylabel)
            ax.legend(fontsize=11)
            if title:
                ax.set_title(title, fontsize=14)
            else:
                ax.set_title(f"$x = {latex(expr_x)},\\; y = {latex(expr_y)}$", fontsize=14)

            filepath = self._save_and_close(fig)
            description = (f"📈 已绘制参数方程 $x = {latex(expr_x)},\\; y = {latex(expr_y)}$ 的图像，"
                           f"t 范围 [{t_min:.2f}, {t_max:.2f}]。")
            await self._send_result(event, description, filepath)
            return description
        except SympifyError as e:
            return f"❌ 表达式解析错误。详情: {e}"
        except Exception as e:
            logger.exception(f"参数方程绘图失败: {e}")
            return f"❌ 绘制参数方程时出错: {e}"

    # ═══════════════════════════════════════════════════
    #  LLM 工具 #7：球坐标参数曲面 r = f(θ, φ)
    # ═══════════════════════════════════════════════════

    async def tool_plot_3d_spherical(
        self, event: AstrMessageEvent,
        expression: str,
        theta_range: str = "", phi_range: str = "", title: str = "",
        xlabel: str = "", ylabel: str = "", zlabel: str = "",
    ) -> MessageEventResult:
        """绘制球坐标参数曲面 r=f(θ,φ)，变量 theta（极角）和 phi（方位角）。
        自动转换为直角坐标并用 plotly 渲染真 3D。
        例如朗伯体辐射分布: cos(theta) 会渲染为一个半球/甜甜圈形发光体。

        Args:
            expression(string): r(θ,φ) 表达式。例如 cos(theta)、1、sin(3*theta)*cos(2*phi)
            theta_range(string): θ 范围 "min,max"，默认 "0,3.1416"
            phi_range(string): φ 范围 "min,max"，默认 "0,6.2832"
            title(string): 图表标题
            xlabel(string): x 轴标签，留空默认 "x"
            ylabel(string): y 轴标签，留空默认 "y"
            zlabel(string): z 轴标签，留空默认 "z"
        """
        try:
            theta_sym = symbols("theta")
            phi_sym = symbols("phi")
            raw = self._preprocess_expr(expression)
            expr = self._parse_expr(raw, extra_symbols=("theta", "phi"))
            f = lambdify((theta_sym, phi_sym), expr, "numpy")

            if theta_range:
                tmin, tmax = self._parse_range(theta_range)
            else:
                tmin, tmax = 0.0, np.pi
            if phi_range:
                pmin, pmax = self._parse_range(phi_range)
            else:
                pmin, pmax = 0.0, 2 * np.pi

            n_theta = self._get_config("plot_3d_grid_density", 201)
            n_phi = n_theta * 2
            theta = np.linspace(tmin, tmax, n_theta)
            phi = np.linspace(pmin, pmax, n_phi)
            T, P = np.meshgrid(theta, phi)

            try:
                R = f(T, P)
                R = np.array(R, dtype=float)
                R[~np.isfinite(R)] = np.nan
            except Exception:
                return f"❌ 无法计算表达式「{expression}」的函数值，请检查定义域。"

            # 球坐标 → 直角坐标
            X = R * np.sin(T) * np.cos(P)
            Y = R * np.sin(T) * np.sin(P)
            Z = R * np.cos(T)

            dpi = self._get_config("plot_dpi", 120)
            cmap = self._get_config("plot_3d_cmap", "viridis")
            filepath = self._render_3d_surface(
                X, Y, Z, cmap_name=cmap,
                elev=self._get_config("plot_3d_elev", 25),
                azim=self._get_config("plot_3d_azim", -60),
                dpi=dpi,
                title=title or f"$r = {latex(expr)}$",
                xlabel=xlabel or "x", ylabel=ylabel or "y", zlabel=zlabel or "z",
            )

            description = (f"📈 已绘制球坐标曲面 $r = {latex(expr)}$ 的 3D 图像，"
                           f"θ 范围 [{tmin:.2f}, {tmax:.2f}]，φ 范围 [{pmin:.2f}, {pmax:.2f}]。")
            await self._send_result(event, description, filepath)
            return description
        except SympifyError as e:
            return f"❌ 表达式解析错误：「{expression}」无法识别。详情: {e}"
        except Exception as e:
            logger.exception(f"球坐标绘图失败: {e}")
            return f"❌ 绘制球坐标曲面时出错: {e}"

    # ═══════════════════════════════════════════════════
    #  LLM 工具 #8：二维矢量场
    # ═══════════════════════════════════════════════════

    async def tool_plot_vector_field_2d(
        self, event: AstrMessageEvent,
        x_expression: str, y_expression: str,
        x_range: str = "", y_range: str = "", title: str = "",
        xlabel: str = "", ylabel: str = "",
    ) -> MessageEventResult:
        """绘制二维矢量场 (Fx(x,y), Fy(x,y)) 的箭头图。用于展示电场方向、流速场、梯度场等。
        箭头长度和方向表示矢量大小和方向。

        Args:
            x_expression(string): Fx(x,y) 表达式。例如 -x/sqrt(x**2+y**2)**3
            y_expression(string): Fy(x,y) 表达式。例如 -y/sqrt(x**2+y**2)**3
            x_range(string): x 范围 "min,max"，默认 "-5,5"
            y_range(string): y 范围，默认同 x_range
            title(string): 图表标题
            xlabel(string): x 轴标签，留空默认 "x"
            ylabel(string): y 轴标签，留空默认 "y"
        """
        try:
            y_sym = symbols("y")
            raw_x = self._preprocess_expr(x_expression)
            raw_y = self._preprocess_expr(y_expression)
            expr_x = self._parse_expr(raw_x, extra_symbols=("y",))
            expr_y = self._parse_expr(raw_y, extra_symbols=("y",))
            fx = lambdify((sym_x, y_sym), expr_x, "numpy")
            fy = lambdify((sym_x, y_sym), expr_y, "numpy")

            if x_range:
                x_min, x_max = self._parse_range(x_range)
            else:
                x_min, x_max = -5.0, 5.0
            if y_range:
                y_min, y_max = self._parse_range(y_range)
            else:
                y_min, y_max = x_min, x_max

            n = 30  # 箭头网格密度
            xs = np.linspace(x_min, x_max, n)
            ys = np.linspace(y_min, y_max, n)
            Xm, Ym = np.meshgrid(xs, ys)

            try:
                U = fx(Xm, Ym)
                V = fy(Xm, Ym)
                U = np.array(U, dtype=float)
                V = np.array(V, dtype=float)
            except Exception:
                return "❌ 无法计算矢量场表达式，请检查定义域。"

            # 过滤 NaN/Inf，并归一化箭头长度以统一显示
            mask = np.isfinite(U) & np.isfinite(V)
            U[~mask], V[~mask] = 0, 0
            mag = np.sqrt(U**2 + V**2)
            mag_max = np.nanmax(mag)
            if mag_max and mag_max > 0:
                U = U / mag_max
                V = V / mag_max

            dpi = self._get_config("plot_dpi", 120)
            fig, ax = self._make_figure()
            ax.quiver(Xm, Ym, U, V, mag, cmap="plasma", scale=30, width=0.003,
                      alpha=0.85, pivot="mid")
            fig.colorbar(plt.cm.ScalarMappable(
                norm=plt.Normalize(vmin=0, vmax=mag_max or 1),
                cmap="plasma"), ax=ax, label="|F|")
            self._style_axes(ax)
            if xlabel: ax.set_xlabel(xlabel)
            if ylabel: ax.set_ylabel(ylabel)
            if title:
                ax.set_title(title, fontsize=14)
            else:
                ax.set_title(f"$\\vec{{F}} = ({latex(expr_x)},\\, {latex(expr_y)})$", fontsize=14)

            filepath = self._save_and_close(fig)
            description = (f"📈 已绘制矢量场 $(F_x, F_y) = ({latex(expr_x)}, {latex(expr_y)})$ 的箭头图，"
                           f"x/y 范围 [{x_min}, {x_max}]。")
            await self._send_result(event, description, filepath)
            return description
        except SympifyError as e:
            return f"❌ 表达式解析错误。详情: {e}"
        except Exception as e:
            logger.exception(f"矢量场绘图失败: {e}")
            return f"❌ 绘制矢量场时出错: {e}"

    # ═══════════════════════════════════════════════════
    #  LLM 工具 #9：多个 3D 曲面叠加
    # ═══════════════════════════════════════════════════

    async def tool_plot_3d_multiple(
        self, event: AstrMessageEvent,
        expressions: str,
        x_range: str = "", y_range: str = "", title: str = "3D 曲面对比",
        xlabel: str = "", ylabel: str = "", zlabel: str = "",
    ) -> MessageEventResult:
        """在同一 3D 坐标系中叠加多个曲面 z=f(x,y)，用于比较不同函数的空间形态。
        每个表达式用不同颜色区分，支持半透明叠加。

        Args:
            expressions(string): 逗号分隔的二元函数表达式。例如 "x**2+y**2, sqrt(x**2+y**2), sin(x)*cos(y)"
            x_range(string): x 范围 "min,max"，默认 "-5,5"
            y_range(string): y 范围，默认同 x_range
            title(string): 图表标题
            xlabel(string): x 轴标签，留空默认 "x"
            ylabel(string): y 轴标签，留空默认 "y"
            zlabel(string): z 轴标签，留空默认 "z"
        """
        try:
            expr_list = [e.strip() for e in expressions.split(";" if ";" in expressions else ",") if e.strip()]
            if len(expr_list) < 2:
                return "❌ 请提供至少 2 个逗号分隔的表达式。"
            if len(expr_list) > 5:
                return "❌ 最多支持 5 个 3D 曲面同时叠加。"

            if x_range:
                x_min, x_max = self._parse_range(x_range)
            else:
                x_min, x_max = -5.0, 5.0
            if y_range:
                y_min, y_max = self._parse_range(y_range)
            else:
                y_min, y_max = x_min, x_max

            grid_density = self._get_config("plot_3d_grid_density", 150)
            xs = np.linspace(x_min, x_max, grid_density)
            ys = np.linspace(y_min, y_max, grid_density)
            X, Y = np.meshgrid(xs, ys)
            dpi = self._get_config("plot_dpi", 120)

            fig = plt.figure(figsize=(12, 9), dpi=dpi, facecolor="white")
            ax = fig.add_subplot(111, projection="3d")
            ax.computed_zorder = False

            colors = ["#2196F3", "#F44336", "#4CAF50", "#FF9800", "#9C27B0"]
            descriptions = []
            y_sym = symbols("y")

            for i, raw_expr in enumerate(expr_list):
                processed = self._preprocess_expr(raw_expr)
                expr = self._parse_expr(processed, extra_symbols=("y",))
                f = lambdify((sym_x, y_sym), expr, "numpy")
                try:
                    Z = f(X, Y)
                    Z = np.array(Z, dtype=float)
                    Z[~np.isfinite(Z)] = np.nan
                except Exception:
                    continue
                ax.plot_surface(X, Y, Z, color=colors[i % len(colors)],
                                alpha=0.6 if i > 0 else 0.85,
                                linewidth=0, antialiased=True,
                                label=f"$z = {latex(expr)}$")
                descriptions.append(f"$z = {latex(expr)}$")

            if not descriptions:
                return "❌ 所有表达式均无法计算。"

            ax.set_xlabel(xlabel or "x", fontsize=10)
            ax.set_ylabel(ylabel or "y", fontsize=10)
            ax.set_zlabel(zlabel or "z", fontsize=10)
            ax.set_title(title, fontsize=14)
            ax.legend(fontsize=9)

            filename = f"plot_{uuid.uuid4().hex[:8]}.png"
            filepath = os.path.join(self.plots_dir, filename)
            fig.savefig(filepath, dpi=dpi, facecolor="white", bbox_inches="tight")
            plt.close(fig)

            desc = "📈 已绘制 3D 曲面对比：" + ", ".join(descriptions) + f"。x/y 范围 [{x_min}, {x_max}]。"
            await self._send_result(event, desc, filepath)
            return desc
        except SympifyError as e:
            return f"❌ 表达式解析错误。详情: {e}"
        except Exception as e:
            logger.exception(f"3D 多曲面绘图失败: {e}")
            return f"❌ 绘制 3D 多曲面时出错: {e}"

    # ═══════════════════════════════════════════════════
    #  LLM 工具 #10：隐式三维曲面 F(x,y,z)=0
    # ═══════════════════════════════════════════════════

    async def tool_plot_implicit_3d(
        self, event: AstrMessageEvent,
        equation: str,
        x_range: str = "", y_range: str = "", z_range: str = "",
        title: str = "",
        xlabel: str = "", ylabel: str = "", zlabel: str = "",
    ) -> MessageEventResult:
        """绘制隐式三维曲面 F(x,y,z)=0，如球面、双曲面、晶体结构等。
        将方程移项为 0 的形式传入。使用 plotly Isosurface 等值面渲染。

        Args:
            equation(string): 移项为 0 的方程。例如球面: "x**2+y**2+z**2-1"，双曲面: "x**2+y**2-z**2-1"
            x_range(string): x 范围 "min,max"，默认 "-3,3"
            y_range(string): y 范围，默认同 x_range
            z_range(string): z 范围，默认同 x_range
            title(string): 图表标题
            xlabel(string): x 轴标签，留空默认 "x"
            ylabel(string): y 轴标签，留空默认 "y"
            zlabel(string): z 轴标签，留空默认 "z"
        """
        try:
            y_sym = symbols("y")
            z_sym = symbols("z")
            raw = self._preprocess_expr(equation)
            locals_dict = {
                "x": sym_x, "y": y_sym, "z": z_sym,
                "sin": __import__("sympy").sin, "cos": __import__("sympy").cos,
                "tan": __import__("sympy").tan, "exp": __import__("sympy").exp,
                "log": __import__("sympy").log, "sqrt": __import__("sympy").sqrt,
                "abs": __import__("sympy").Abs, "pi": __import__("sympy").pi,
                "E": __import__("sympy").E,
            }
            expr = sympify(raw, locals=locals_dict)
            f = lambdify((sym_x, y_sym, z_sym), expr, "numpy")

            if x_range:
                x_min, x_max = self._parse_range(x_range)
            else:
                x_min, x_max = -3.0, 3.0
            if y_range:
                y_min, y_max = self._parse_range(y_range)
            else:
                y_min, y_max = x_min, x_max
            if z_range:
                z_min, z_max = self._parse_range(z_range)
            else:
                z_min, z_max = x_min, x_max

            n_xy = self._get_config("plot_3d_grid_density", 120)
            n_z = 60
            xs = np.linspace(x_min, x_max, n_xy)
            ys = np.linspace(y_min, y_max, n_xy)
            zs = np.linspace(z_min, z_max, n_z)
            X2d, Y2d = np.meshgrid(xs, ys)

            dpi = self._get_config("plot_dpi", 120)
            fig = plt.figure(figsize=(12, 9), dpi=dpi, facecolor="white")
            ax = fig.add_subplot(111, projection="3d")
            ax.computed_zorder = False

            # 在每个 z 切片上绘制 F(x,y,z)=0 的等高线
            for z_val in zs:
                try:
                    Zslice = np.full_like(X2d, z_val)
                    V_slice = f(X2d, Y2d, Zslice)
                    V_slice = np.array(V_slice, dtype=float)
                    ax.contour(X2d, Y2d, V_slice, levels=[0], colors="#2196F3",
                               linewidths=0.6, alpha=0.5, zdir="z", offset=z_val)
                except Exception:
                    continue

            ax.set_xlabel(xlabel or "x", fontsize=10)
            ax.set_ylabel(ylabel or "y", fontsize=10)
            ax.set_zlabel(zlabel or "z", fontsize=10)
            ax.set_title(title or f"${latex(expr)} = 0$", fontsize=14)

            filename = f"plot_{uuid.uuid4().hex[:8]}.png"
            filepath = os.path.join(self.plots_dir, filename)
            fig.savefig(filepath, dpi=dpi, facecolor="white", bbox_inches="tight")
            plt.close(fig)

            description = (f"📈 已绘制隐式曲面 ${latex(expr)} = 0$ 的 3D 图像，"
                           f"范围 x[{x_min},{x_max}] y[{y_min},{y_max}] z[{z_min},{z_max}]。")
            await self._send_result(event, description, filepath)
            return description
        except SympifyError as e:
            return f"❌ 表达式解析错误：「{equation}」无法识别。详情: {e}"
        except Exception as e:
            logger.exception(f"隐式 3D 绘图失败: {e}")
            return f"❌ 绘制隐式 3D 曲面时出错: {e}"

    # ═══════════════════════════════════════════════════
    #  LLM 工具 #11：三维参数曲线
    # ═══════════════════════════════════════════════════

    async def tool_plot_3d_parametric(
        self, event: AstrMessageEvent,
        x_expression: str, y_expression: str, z_expression: str,
        t_range: str = "", title: str = "",
        xlabel: str = "", ylabel: str = "", zlabel: str = "",
    ) -> MessageEventResult:
        """绘制三维参数曲线 (x(t), y(t), z(t))。用于展示空间螺线、轨迹、光线路径等。
        变量为 t。使用 matplotlib 3D 渲染。

        Args:
            x_expression(string): x(t) 表达式。例如 cos(t)、sin(2*t)
            y_expression(string): y(t) 表达式。例如 sin(t)、cos(3*t)
            z_expression(string): z(t) 表达式。例如 t、t/5
            t_range(string): t 范围 "min,max"，默认 "0,12.5664"（0 到 4π）
            title(string): 图表标题
            xlabel(string): x 轴标签，留空默认 "x"
            ylabel(string): y 轴标签，留空默认 "y"
            zlabel(string): z 轴标签，留空默认 "z"
        """
        try:
            t_sym = symbols("t")
            raw_x = self._preprocess_expr(x_expression)
            raw_y = self._preprocess_expr(y_expression)
            raw_z = self._preprocess_expr(z_expression)
            expr_x = self._parse_expr(raw_x, extra_symbols=("t",))
            expr_y = self._parse_expr(raw_y, extra_symbols=("t",))
            expr_z = self._parse_expr(raw_z, extra_symbols=("t",))
            fx = lambdify(t_sym, expr_x, "numpy")
            fy = lambdify(t_sym, expr_y, "numpy")
            fz = lambdify(t_sym, expr_z, "numpy")

            if t_range:
                t_min, t_max = self._parse_range(t_range)
            else:
                t_min, t_max = 0.0, 4 * np.pi

            t_vals = np.linspace(t_min, t_max, 2000)
            try:
                x_vals = np.array(fx(t_vals), dtype=float)
                y_vals = np.array(fy(t_vals), dtype=float)
                z_vals = np.array(fz(t_vals), dtype=float)
            except Exception:
                return "❌ 无法计算表达式的函数值，请检查定义域。"

            mask = np.isfinite(x_vals) & np.isfinite(y_vals) & np.isfinite(z_vals)
            x_vals, y_vals, z_vals = x_vals[mask], y_vals[mask], z_vals[mask]
            if len(x_vals) == 0:
                return "❌ 参数方程在指定范围内无有效值。"

            dpi = self._get_config("plot_dpi", 120)
            fig = plt.figure(figsize=(12, 9), dpi=dpi, facecolor="white")
            ax = fig.add_subplot(111, projection="3d")
            ax.computed_zorder = False

            ax.plot(x_vals, y_vals, z_vals, linewidth=1.5, color="#2196F3")
            ax.set_xlabel(xlabel or "x", fontsize=10)
            ax.set_ylabel(ylabel or "y", fontsize=10)
            ax.set_zlabel(zlabel or "z", fontsize=10)
            ax.set_title(title or f"$(x,y,z) = ({latex(expr_x)},\\, {latex(expr_y)},\\, {latex(expr_z)})$", fontsize=14)

            filename = f"plot_{uuid.uuid4().hex[:8]}.png"
            filepath = os.path.join(self.plots_dir, filename)
            fig.savefig(filepath, dpi=dpi, facecolor="white", bbox_inches="tight")
            plt.close(fig)

            description = (f"📈 已绘制三维参数曲线 $(x,y,z) = ({latex(expr_x)},\\, {latex(expr_y)},\\, {latex(expr_z)})$，"
                           f"t 范围 [{t_min:.2f}, {t_max:.2f}]。")
            await self._send_result(event, description, filepath)
            return description
        except SympifyError as e:
            return f"❌ 表达式解析错误。详情: {e}"
        except Exception as e:
            logger.exception(f"3D 参数曲线绘图失败: {e}")
            return f"❌ 绘制 3D 参数曲线时出错: {e}"


    # ═══════════════════════════════════════════════════
    #  LLM 工具 #12：二维向量图
    # ═══════════════════════════════════════════════════

    async def tool_plot_vector_2d(
        self, event: AstrMessageEvent,
        vectors: str, title: str = "",
        xlabel: str = "", ylabel: str = "",
    ) -> MessageEventResult:
        """绘制二维向量示意图。每个向量用带实心三角箭头的线段表示（箭头在终点）。

        Args:
            vectors(string): 向量定义，多个向量用英文分号 ";" 分隔。每个向量格式：
                "x,y :颜色:标签" → 从原点出发
                "(x1,y1)->(x2,y2) :颜色:标签" → 指定起终点
                例："3,4:red:v1 ; 0,0->2,3:blue:v2"
            title(string): 图表标题，留空自动生成
            xlabel(string): x 轴标签，留空默认 "x"
            ylabel(string): y 轴标签，留空默认 "y"
        """
        try:
            vector_list = [v.strip() for v in vectors.split(";") if v.strip()]
            if not vector_list:
                return "❌ 请提供至少一个向量定义。例如 vectors=\"3,4\""
            title = self._sanitize_math_text(title)
            xlabel = self._sanitize_math_text(xlabel)
            ylabel = self._sanitize_math_text(ylabel)

            fig, ax = self._make_figure()
            lw = self._get_config("line_width", 2.0)
            all_x, all_y = [], []

            for vec_str in vector_list:
                vd = self._parse_vector_def(vec_str)
                self._draw_2d_arrow(ax, vd["start"], vd["end"], vd["color"], lw, vd["label"])
                all_x.extend([vd["start"][0], vd["end"][0]])
                all_y.extend([vd["start"][1], vd["end"][1]])

            self._style_axes(ax)
            if xlabel: ax.set_xlabel(xlabel)
            if ylabel: ax.set_ylabel(ylabel)
            ax.set_title(title or "二维向量图", fontsize=14)
            margin = max(max(all_x) - min(all_x), max(all_y) - min(all_y), 4) * 0.2 + 1
            ax.set_xlim(min(all_x) - margin, max(all_x) + margin)
            ax.set_ylim(min(all_y) - margin, max(all_y) + margin)
            ax.set_aspect("equal")

            filepath = self._save_and_close(fig)
            description = f"📐 已绘制二维向量图（{len(vector_list)} 个向量）。"
            await self._send_result(event, description, filepath)
            return description
        except ValueError as e:
            return f"❌ 向量格式错误: {e}"
        except Exception as e:
            logger.exception(f"二维向量绘图失败: {e}")
            return f"❌ 绘制二维向量图时出错: {e}"

    # ═══════════════════════════════════════════════════
    #  LLM 工具 #13：三维向量图
    # ═══════════════════════════════════════════════════

    async def tool_plot_vector_3d(
        self, event: AstrMessageEvent,
        vectors: str, title: str = "",
        xlabel: str = "", ylabel: str = "", zlabel: str = "",
    ) -> MessageEventResult:
        """绘制三维向量示意图。每个向量用线段 + 末端锥体箭头表示。

        Args:
            vectors(string): 向量定义，多个向量用英文分号 ";" 分隔。格式：
                "x,y,z :颜色:标签" → 从原点出发
                "(x1,y1,z1)->(x2,y2,z2) :颜色:标签" → 指定起终点
                例："1,2,3:red:v1 ; 0,0,0->3,4,1:blue:v2"
            title(string): 图表标题
            xlabel(string): x 轴标签，留空默认 "x"
            ylabel(string): y 轴标签，留空默认 "y"
            zlabel(string): z 轴标签，留空默认 "z"
        """
        try:
            vector_list = [v.strip() for v in vectors.split(";") if v.strip()]
            if not vector_list:
                return "❌ 请提供至少一个三维向量定义。"
            title = self._sanitize_math_text(title)
            xlabel = self._sanitize_math_text(xlabel)
            ylabel = self._sanitize_math_text(ylabel)
            zlabel = self._sanitize_math_text(zlabel)

            parsed = [self._parse_vector_def_3d(v) for v in vector_list]
            dpi = self._get_config("plot_dpi", 120)
            plot_title = title or "三维向量图"

            filepath = self._render_3d_vectors(
                parsed, plot_title, dpi,
                xlabel=xlabel or "x", ylabel=ylabel or "y", zlabel=zlabel or "z",
            )
            description = f"📐 已绘制三维向量图（{len(parsed)} 个向量）。"
            await self._send_result(event, description, filepath)
            return description
        except ValueError as e:
            return f"❌ 向量格式错误: {e}"
        except Exception as e:
            logger.exception(f"三维向量绘图失败: {e}")
            return f"❌ 绘制三维向量图时出错: {e}"

    # ═══════════════════════════════════════════════════
    #  LLM 工具 #14：向量加法演示（平行四边形法则）
    # ═══════════════════════════════════════════════════

    async def tool_plot_vector_addition(
        self, event: AstrMessageEvent,
        v1: str, v2: str, title: str = "",
        xlabel: str = "", ylabel: str = "",
    ) -> MessageEventResult:
        """演示向量加法 v1 + v2，用平行四边形法则显示结果向量。

        Args:
            v1(string): 第一个向量分量，格式 "x,y"。例如 "1,2"
            v2(string): 第二个向量分量，格式 "x,y"。例如 "3,1"
            title(string): 图表标题，留空自动生成
            xlabel(string): x 轴标签
            ylabel(string): y 轴标签
        """
        try:
            title = self._sanitize_math_text(title)
            xlabel = self._sanitize_math_text(xlabel)
            ylabel = self._sanitize_math_text(ylabel)
            p_v1 = self._parse_point(v1)
            p_v2 = self._parse_point(v2)
            v1x, v1y = p_v1
            v2x, v2y = p_v2
            # 结果向量
            rx, ry = v1x + v2x, v1y + v2y

            fig, ax = self._make_figure()
            lw = self._get_config("line_width", 2.0)

            # v1（蓝）
            self._draw_2d_arrow(ax, (0, 0), (v1x, v1y), "#2196F3", lw,
                                f"v1=({v1x},{v1y})", (0.12, -0.12))
            # v2（红）
            self._draw_2d_arrow(ax, (0, 0), (v2x, v2y), "#F44336", lw,
                                f"v2=({v2x},{v2y})", (0.12, -0.12))
            # 平行四边形辅助线（虚线）：从 v1 终点平移 v2，从 v2 终点平移 v1
            ax.plot([v1x, rx], [v1y, ry], linestyle="--", linewidth=1.2,
                    color="#9E9E9E", zorder=2)
            ax.plot([v2x, rx], [v2y, ry], linestyle="--", linewidth=1.2,
                    color="#9E9E9E", zorder=2)
            # 结果向量 v1+v2（绿）
            self._draw_2d_arrow(ax, (0, 0), (rx, ry), "#4CAF50", lw + 1,
                                f"v1+v2=({rx},{ry})", (0.15, -0.15))

            self._style_axes(ax)
            if xlabel: ax.set_xlabel(xlabel)
            if ylabel: ax.set_ylabel(ylabel)
            ax.set_title(title or f"向量加法: v1+v2 = ({rx},{ry})", fontsize=15)
            all_x = [0, v1x, v2x, rx]
            all_y = [0, v1y, v2y, ry]
            x_margin = (max(all_x) - min(all_x)) * 0.2 + 1.5
            y_margin = (max(all_y) - min(all_y)) * 0.2 + 1.5
            ax.set_xlim(min(all_x) - x_margin, max(all_x) + x_margin)
            ax.set_ylim(min(all_y) - y_margin, max(all_y) + y_margin)
            ax.set_aspect("equal")

            filepath = self._save_and_close(fig)
            description = f"📐 已绘制向量加法演示：v1=({v1x},{v1y}) + v2=({v2x},{v2y}) = ({rx},{ry})。"
            await self._send_result(event, description, filepath)
            return description
        except ValueError as e:
            return f"❌ 向量格式错误: {e}"
        except Exception as e:
            logger.exception(f"向量加法绘图失败: {e}")
            return f"❌ 绘制向量加法时出错: {e}"

    # ═══════════════════════════════════════════════════
    #  LLM 工具 #15：向量数乘演示
    # ═══════════════════════════════════════════════════

    async def tool_plot_vector_scaling(
        self, event: AstrMessageEvent,
        vector: str, scalar: float = 2.0, title: str = "",
        xlabel: str = "", ylabel: str = "",
    ) -> MessageEventResult:
        """演示向量数乘 k·v，同时显示原向量和缩放后的向量（颜色区分）。

        Args:
            vector(string): 向量分量，格式 "x,y"。例如 "1,3"
            scalar(number): 标量 k，默认 2.0。支持负数（反向）、小数（缩短）
            title(string): 图表标题
            xlabel(string): x 轴标签
            ylabel(string): y 轴标签
        """
        try:
            title = self._sanitize_math_text(title)
            xlabel = self._sanitize_math_text(xlabel)
            ylabel = self._sanitize_math_text(ylabel)
            px, py = self._parse_point(vector)
            k = float(scalar)
            sx, sy = k * px, k * py

            fig, ax = self._make_figure()
            lw = self._get_config("line_width", 2.0)

            # 原向量（灰色虚线 + 实心箭）
            self._draw_2d_arrow(ax, (0, 0), (px, py), "#9E9E9E", lw,
                                f"v=({px},{py})", (0.12, -0.12))
            # 缩放后向量（橙/紫色）
            sc_color = "#FF9800" if k >= 0 else "#9C27B0"
            self._draw_2d_arrow(ax, (0, 0), (sx, sy), sc_color, lw + 1,
                                f"{k}v=({sx:.1f},{sy:.1f})", (0.15, -0.15))

            self._style_axes(ax)
            if xlabel: ax.set_xlabel(xlabel)
            if ylabel: ax.set_ylabel(ylabel)
            ax.set_title(title or f"向量数乘: {k}·v = ({sx:.1f},{sy:.1f})", fontsize=15)
            all_x = [0, px, sx]
            all_y = [0, py, sy]
            x_margin = (max(all_x) - min(all_x)) * 0.2 + 1.5
            y_margin = (max(all_y) - min(all_y)) * 0.2 + 1.5
            ax.set_xlim(min(all_x) - x_margin, max(all_x) + x_margin)
            ax.set_ylim(min(all_y) - y_margin, max(all_y) + y_margin)
            ax.set_aspect("equal")

            filepath = self._save_and_close(fig)
            description = f"📐 已绘制向量数乘演示：{k}·v = ({sx:.1f},{sy:.1f})。"
            await self._send_result(event, description, filepath)
            return description
        except ValueError as e:
            return f"❌ 向量格式错误: {e}"
        except Exception as e:
            logger.exception(f"向量数乘绘图失败: {e}")
            return f"❌ 绘制向量数乘时出错: {e}"

    # ═══════════════════════════════════════════════════
    #  LLM 工具 #16：基变换演示
    # ═══════════════════════════════════════════════════

    async def tool_plot_basis_transform(
        self, event: AstrMessageEvent,
        new_basis: str, title: str = "",
        xlabel: str = "", ylabel: str = "",
    ) -> MessageEventResult:
        """演示基变换：在同一坐标系中显示标准基向量 e1, e2 和一组新基向量。

        Args:
            new_basis(string): 新基向量，格式 "(b1x,b1y),(b2x,b2y)"。
                例："(1,1),(1,-1)" 表示新基 b1=(1,1), b2=(1,-1)
            title(string): 图表标题
            xlabel(string): x 轴标签
            ylabel(string): y 轴标签
        """
        try:
            title = self._sanitize_math_text(title)
            xlabel = self._sanitize_math_text(xlabel)
            ylabel = self._sanitize_math_text(ylabel)
            s = new_basis.strip()
            # 匹配 (a,b),(c,d) 格式
            pattern = r"\(\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*\)"
            matches = re.findall(pattern, s)
            if len(matches) != 2:
                return "❌ 请提供两个基向量，格式: \"(b1x,b1y),(b2x,b2y)\"。例: \"(1,1),(1,-1)\""
            b1 = (float(matches[0][0]), float(matches[0][1]))
            b2 = (float(matches[1][0]), float(matches[1][1]))

            fig, ax = self._make_figure()
            lw = self._get_config("line_width", 2.0)

            # 标准基 e1, e2（灰色）
            self._draw_2d_arrow(ax, (0, 0), (1, 0), "#9E9E9E", lw,
                                "e1=(1,0)", (-0.1, -0.25))
            self._draw_2d_arrow(ax, (0, 0), (0, 1), "#9E9E9E", lw,
                                "e2=(0,1)", (-0.35, -0.05))
            # 新基 b1（红）, b2（蓝）
            self._draw_2d_arrow(ax, (0, 0), b1, "#F44336", lw + 1,
                                f"b1=({b1[0]},{b1[1]})", (0.12, -0.15))
            self._draw_2d_arrow(ax, (0, 0), b2, "#2196F3", lw + 1,
                                f"b2=({b2[0]},{b2[1]})", (0.12, -0.15))

            self._style_axes(ax)
            if xlabel: ax.set_xlabel(xlabel)
            if ylabel: ax.set_ylabel(ylabel)
            ax.set_title(title or f"基变换: 标准基 -> b1=({b1[0]},{b1[1]}), b2=({b2[0]},{b2[1]})", fontsize=15)
            all_x = [0, 1, b1[0], b2[0]]
            all_y = [0, 1, b1[1], b2[1]]
            x_margin = (max(all_x) - min(all_x)) * 0.2 + 1.5
            y_margin = (max(all_y) - min(all_y)) * 0.2 + 1.5
            ax.set_xlim(min(all_x) - x_margin, max(all_x) + x_margin)
            ax.set_ylim(min(all_y) - y_margin, max(all_y) + y_margin)
            ax.set_aspect("equal")

            filepath = self._save_and_close(fig)
            description = (f"📐 已绘制基变换演示：标准基 e1=(1,0), e2=(0,1) "
                           f"→ 新基 b1=({b1[0]},{b1[1]}), b2=({b2[0]},{b2[1]})。")
            await self._send_result(event, description, filepath)
            return description
        except ValueError as e:
            return f"❌ 基向量格式错误: {e}"
        except Exception as e:
            logger.exception(f"基变换绘图失败: {e}")
            return f"❌ 绘制基变换时出错: {e}"
