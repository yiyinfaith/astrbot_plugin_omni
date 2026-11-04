"""今日人品（原 jrrp）与 LLM 四则运算工具（原 primermath）。"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta, timezone

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


def _fmt(value: float) -> str:
    value = round(float(value), 3)
    return str(value)


def arithmetic(op: str, num1: float, num2: float) -> str:
    prefix = "按照你的人设回答用户结果是:"
    num1, num2 = float(num1), float(num2)
    if op == "add":
        return f"{prefix}{_fmt(num1 + num2)}。"
    if op == "minus":
        return f"{prefix}{_fmt(num1 - num2)}。"
    if op == "mul":
        return f"{prefix}{_fmt(num1 * num2)}。"
    if op == "div":
        if num2 == 0:
            return f"{prefix}除数不能为 0，这道题没有结果。"
        return f"{prefix}{_fmt(num1 / num2)}。"
    if num1 > num2:
        return f"{prefix}第一个数字大于第二个数字。"
    if num1 < num2:
        return f"{prefix}第一个数字小于第二个数字。"
    return f"{prefix}这是同一个数字。"
