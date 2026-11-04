"""今日人品（原 jrrp）与 LLM 精确计算工具（原 primermath）。"""

from __future__ import annotations

import ast
import hashlib
import math
import operator
import random
from datetime import datetime, timedelta, timezone
from fractions import Fraction

BEIJING = timezone(timedelta(hours=8))

FORTUNE_DESCRIPTIONS = (
    (1, 10, "今天是不太顺利的一天，建议多加小心！"),
    (11, 30, "今天的运势一般，适合做一些小事情！"),
    (31, 60, "今天的运势不错，可以尝试一些新的事物！"),
    (61, 80, "今天的运势很好，适合做一些大事情！"),
    (81, 100, "今天的运势非常好，万事如意！"),
)


def jrrp(user_id: str, user_name: str, now: datetime | None = None) -> str:
    """同一用户同一天（北京时间）结果固定，范围 1-100。"""
    now = now or datetime.now(BEIJING)
    seed = hashlib.sha256(f"{now:%Y-%m-%d}:{user_id}".encode()).hexdigest()
    rp = random.Random(seed).randint(1, 100)
    desc = next((d for low, high, d in FORTUNE_DESCRIPTIONS if low <= rp <= high), "今天的运势未知，请自行判断！")
    return f"{user_name}，你今天的人品是{rp}，{desc}"


PREFIX = "按照你的人设回答用户结果是:"
_FUNCS = {
    "sqrt": math.sqrt, "abs": abs, "round": round, "log": math.log, "log10": math.log10,
    "exp": math.exp, "sin": math.sin, "cos": math.cos, "tan": math.tan, "floor": math.floor,
    "ceil": math.ceil, "min": min, "max": max, "pow": pow,
}
_CONSTS = {"pi": math.pi, "e": math.e}
_BINOPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_CMPOPS = {
    ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.Eq: operator.eq, ast.NotEq: operator.ne,
}


MAX_DIGITS = 10000


def _pow_digits(base, exponent) -> float:
    """估算乘方结果的位数，防止 (9**1000)**1000 这类算式算到卡死。"""
    try:
        base = abs(Fraction(base))
        if base in (0, 1):
            return 0.0
        size = abs(math.log10(base.numerator) - math.log10(base.denominator))
        return size * abs(float(exponent))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return Fraction(str(node.value)) if isinstance(node.value, float) else Fraction(node.value)
    if isinstance(node, ast.Name) and node.id in _CONSTS:
        return _CONSTS[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval(node.operand)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ValueError("指数太大")
        if isinstance(node.op, ast.Pow) and _pow_digits(left, right) > MAX_DIGITS:
            raise ValueError("结果太大")
        if isinstance(node.op, ast.Pow) and isinstance(right, Fraction) and right.denominator != 1:
            left, right = float(left), float(right)
        return _BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.Compare) and all(type(op) in _CMPOPS for op in node.ops):
        left = _eval(node.left)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval(comp)
            if not _CMPOPS[type(op)](left, right):
                return False
            left = right
        return True
    if (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id in _FUNCS and not node.keywords
    ):
        args = [_eval(a) for a in node.args]
        if node.func.id in ("abs", "min", "max"):
            return _FUNCS[node.func.id](*args)
        if node.func.id == "round":
            return Fraction(round(float(args[0]), int(args[1]) if len(args) > 1 else 0))
        return _FUNCS[node.func.id](*[float(a) for a in args])
    raise ValueError("只支持数字、+ - * / // % **、括号、比较和常用数学函数")


def _fmt(value) -> str:
    if isinstance(value, bool):
        return "成立" if value else "不成立"
    if isinstance(value, Fraction):
        if value.denominator == 1:
            digits = str(abs(value.numerator))
            if len(digits) <= 30:
                return str(value.numerator)
            sign = "-" if value < 0 else ""
            return f"{sign}{digits[0]}.{digits[1:10].rstrip('0') or '0'}e+{len(digits) - 1}"
        try:
            value = float(value)
        except OverflowError:
            raise ValueError("结果太大") from None
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("结果不是有限数")
        text = f"{value:.10g}"
        return text
    return str(value)


def calculate(expression: str) -> str:
    """安全地计算算式；整数和小数的加减乘除按精确分数计算，避免浮点误差。"""
    expr = (expression or "").strip().replace("×", "*").replace("÷", "/").replace("^", "**")
    expr = expr.replace("（", "(").replace("）", ")").replace("，", ",")
    if not expr:
        return f"{PREFIX}没有给出算式。"
    if len(expr) > 300:
        return f"{PREFIX}算式太长，无法计算。"
    try:
        result = _eval(ast.parse(expr, mode="eval"))
        return f"{PREFIX}{_fmt(result)}。"
    except ZeroDivisionError:
        return f"{PREFIX}除数不能为 0，这道题没有结果。"
    except (ValueError, TypeError, SyntaxError, OverflowError) as exc:
        return f"{PREFIX}无法计算「{expression}」：{exc}"
