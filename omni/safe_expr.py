"""数学表达式安全检查。

sympify 内部会 eval 字符串，表达式又来自 LLM（可被用户诱导），必须先检查再交给 sympy：
只允许数字、白名单里的变量和函数、算术与比较运算；纯数字的乘方限制大小，避免 9**9**9 卡死机器人。
"""

from __future__ import annotations

import ast
import operator

from sympy import SympifyError
from sympy import sympify as _sympify

MAX_LENGTH = 500
MAX_NUMERIC_EXPONENT = 1000
NAMES = {
    "x", "y", "z", "t", "theta", "phi",
    "pi", "E", "e", "True", "False",
}
FUNCTIONS = {
    "sin", "cos", "tan", "asin", "acos", "atan", "sinh", "cosh", "tanh",
    "exp", "log", "ln", "sqrt", "abs", "Abs", "Max", "Min", "Heaviside", "sign",
    "Piecewise", "floor", "ceiling",
}
_BIN_OPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod, ast.BitAnd, ast.BitOr)
_CMP_OPS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq)
_ARITH = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


class UnsafeExpression(SympifyError):
    """归入 SympifyError，绘图函数会把它当作“表达式解析错误”提示给用户。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


def _is_numeric(node: ast.AST) -> bool:
    return not any(isinstance(n, (ast.Name, ast.Call)) for n in ast.walk(node))


def _numeric_value(node: ast.AST) -> float:
    """计算纯数字指数的值（只允许加减乘除），用浮点数不会卡住。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _numeric_value(node.operand)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp) and type(node.op) in _ARITH:
        try:
            return _ARITH[type(node.op)](_numeric_value(node.left), _numeric_value(node.right))
        except (ZeroDivisionError, OverflowError):
            return float("inf")
    raise UnsafeExpression("纯数字的指数里只能有加减乘除")


def _check(node: ast.AST) -> None:
    if isinstance(node, ast.Expression):
        _check(node.body)
    elif isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float, bool)):
            raise UnsafeExpression("表达式里不能有字符串")
    elif isinstance(node, ast.Name):
        if node.id not in NAMES and node.id not in FUNCTIONS:
            raise UnsafeExpression(f"不支持的名称「{node.id}」")
    elif isinstance(node, ast.BinOp):
        if not isinstance(node.op, _BIN_OPS):
            raise UnsafeExpression("不支持的运算符")
        if isinstance(node.op, ast.Pow) and _is_numeric(node.right):
            if any(isinstance(n, ast.BinOp) and isinstance(n.op, ast.Pow) for n in ast.walk(node.right)):
                raise UnsafeExpression("不支持连续乘方")
            if abs(_numeric_value(node.right)) > MAX_NUMERIC_EXPONENT:
                raise UnsafeExpression(f"指数不能超过 {MAX_NUMERIC_EXPONENT}")
            if _is_numeric(node.left) and any(
                isinstance(n, ast.BinOp) and isinstance(n.op, ast.Pow) for n in ast.walk(node.left)
            ):
                raise UnsafeExpression("不支持连续乘方")
        _check(node.left)
        _check(node.right)
    elif isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, (ast.USub, ast.UAdd, ast.Invert, ast.Not)):
            raise UnsafeExpression("不支持的运算符")
        _check(node.operand)
    elif isinstance(node, ast.Compare):
        if not all(isinstance(op, _CMP_OPS) for op in node.ops):
            raise UnsafeExpression("不支持的比较运算")
        _check(node.left)
        for item in node.comparators:
            _check(item)
    elif isinstance(node, ast.BoolOp):
        for item in node.values:
            _check(item)
    elif isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS or node.keywords:
            raise UnsafeExpression("不支持的函数调用")
        for arg in node.args:
            _check(arg)
    elif isinstance(node, ast.Tuple):
        for item in node.elts:
            _check(item)
    else:
        raise UnsafeExpression("表达式里含有不支持的语法")


def check_expression(expression: str) -> None:
    """不安全或不支持时抛出 UnsafeExpression。"""
    if not isinstance(expression, str):
        raise UnsafeExpression("表达式必须是文本")
    if len(expression) > MAX_LENGTH:
        raise UnsafeExpression(f"表达式过长（最多 {MAX_LENGTH} 个字符）")
    try:
        tree = ast.parse(expression.replace("^", "**").strip(), mode="eval")
    except SyntaxError as exc:
        raise UnsafeExpression("表达式语法错误") from exc
    _check(tree)


def safe_sympify(expression, *args, **kwargs):
    """先做安全检查再交给 sympy.sympify；非字符串（已是 sympy 对象或数字）直接放行。"""
    if isinstance(expression, str):
        check_expression(expression)
    return _sympify(expression, *args, **kwargs)
