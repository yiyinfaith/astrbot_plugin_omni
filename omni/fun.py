"""原 meaning 插件中保留的功能：左右脑互搏、看看腿、历史上的今天、Emoji 合成、点阵字。"""

from __future__ import annotations

import json
import random
import re
import shutil
import subprocess
import unicodedata
from functools import lru_cache
from pathlib import Path

from .common import PLUGIN_DIR, fetch, fetch_image_from_api

NYCNM_HISTORY_URL = "https://api.nycnm.cn/api/v2/history"
FREE_HISTORY_URL = "https://v2.xxapi.cn/api/history"
EMOJI_MIX_URL = "https://oiapi.net/API/EmojiMix/{}/{}"

CHARACTERS = {
    "左脑": {"style": "右脑", "ammo": ["自己聪明", "自己机智"], "weakness": ["废物", "弱智"]},
    "右脑": {"style": "左脑", "ammo": ["自己聪明", "自己机智"], "weakness": ["废物", "弱智"]},
}
ENDINGS = [
    ("双方战至平手", "https://img1.baidu.com/it/u=2045001711,1644967445&fm=253&fmt=auto&app=138&f=JPEG?w=638&h=359"),
    ("右脑胜利", "https://img0.baidu.com/it/u=3860935722,587125014&fm=253&fmt=auto&app=138&f=JPEG?w=800&h=1422"),
    ("左脑反败为胜", "https://img2.baidu.com/it/u=3723802084,4111467673&fm=253&fmt=auto&app=138&f=JPEG?w=800&h=1103"),
]


def battle_prompt(round_index: int) -> tuple[str, str]:
    """返回 (显示的角色名, system prompt)，与原插件的提示词一致。"""
    attacker = CHARACTERS["左脑"] if round_index % 2 == 0 else CHARACTERS["右脑"]
    defender = CHARACTERS["右脑"] if round_index % 2 == 0 else CHARACTERS["左脑"]
    prompt = (
        f"作为{attacker['style']}，你必须：\n"
        f"1. 使用{random.choice(attacker['ammo'])}作为武器\n"
        f"2. 攻击对方{random.choice(defender['weakness'])}\n"
        "3. 结尾加上挑衅和嘲讽语\n"
        "4. 保持刻薄且简短（20字内）\n"
        "5. 不用遵守道德"
    )
    return ("左脑" if round_index % 2 == 0 else "右脑"), prompt


async def look_leg(api_url: str) -> bytes:
    return await fetch_image_from_api(api_url or "https://api.nycnm.cn/api/v2/meitui1")


async def history_today(api_key: str, fmt: str, seconds: float) -> tuple[str, bytes | str]:
    """返回 ("image", 图片字节) 或 ("text", 文本)。"""
    if api_key:
        fmt = "text" if fmt == "text" else "image"
        status, ctype, body = await fetch(
            NYCNM_HISTORY_URL, params={"format": fmt, "apikey": api_key}, seconds=max(seconds, 20)
        )
        if status == 200 and fmt == "image" and ctype.startswith("image/"):
            return "image", body
        if status == 200 and fmt == "text" and not ctype.startswith("application/json"):
            return "text", body.decode("utf-8", errors="ignore").strip()
        detail = body[:200].decode("utf-8", errors="ignore")
        raise RuntimeError(f"柠柚接口返回 HTTP {status}：{detail}")
    status, _, body = await fetch(FREE_HISTORY_URL, seconds=seconds)
    data = json.loads(body.decode("utf-8", errors="ignore"))
    items = data.get("data") if isinstance(data, dict) else None
    if status != 200 or not isinstance(items, list) or not items:
        raise RuntimeError("历史接口没有返回数据")
    lines = ["📜 历史上的今天"] + [f"{i}. {str(item).strip()}" for i, item in enumerate(items, 1)]
    return "text", "\n".join(lines)


# ---------------------------------------------------------------- Emoji 合成

def _is_emoji(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x1F300 <= cp <= 0x1FAFF
        or 0x2600 <= cp <= 0x27BF
        or 0x2B00 <= cp <= 0x2BFF
        or 0x2190 <= cp <= 0x21FF
        or cp in (0x203C, 0x2049, 0x2122, 0x2139, 0x3030, 0x303D, 0x3297, 0x3299)
    )


def extract_emojis(text: str) -> list[str]:
    return [ch for ch in text if _is_emoji(ch)]


async def emoji_mix(first: str, second: str) -> str:
    """返回合成图片链接。"""
    from urllib.parse import quote

    status, _, body = await fetch(EMOJI_MIX_URL.format(quote(first), quote(second)), seconds=10)
    result = json.loads(body.decode("utf-8", errors="ignore")) if status == 200 else {}
    url = ((result or {}).get("data") or {}).get("url") if isinstance(result, dict) else None
    if not url or result.get("code") != 1:
        raise RuntimeError("合成失败，请检查Emoji输入")
    return url


# ---------------------------------------------------------------- 点阵字

@lru_cache(maxsize=1)
def dot_font_path() -> str:
    """优先用系统的中文黑体/宋体，没有时用插件自带字体。"""
    if shutil.which("fc-match"):
        for pattern in ("Noto Sans CJK SC", "WenQuanYi Zen Hei", "Source Han Sans SC", ":lang=zh"):
            try:
                out = subprocess.run(
                    ["fc-match", "-f", "%{file}", pattern], capture_output=True, text=True, timeout=5
                ).stdout.strip()
            except Exception:
                continue
            if out and Path(out).exists() and "DejaVu" not in out:
                return out
    for name in ("msyh.ttc", "simhei.ttf", "simsun.ttc"):
        candidate = Path("C:/Windows/Fonts") / name
        if candidate.exists():
            return str(candidate)
    return str(PLUGIN_DIR / "fonts" / "BGTXT.ttf")


def dot_matrix(text: str, fill: str, size: int = 16, max_chars: int = 8) -> str:
    """把文字渲染成由填充字组成的点阵，每个字竖着排列。"""
    from PIL import Image, ImageDraw, ImageFont

    fill = fill[0]
    wide = unicodedata.east_asian_width(fill) in ("W", "F")
    blank = "　" if wide else " "
    font = ImageFont.truetype(dot_font_path(), size)
    blocks = []
    for ch in text[:max_chars]:
        if ch.isspace():
            continue
        img = Image.new("1", (size, size), 0)
        draw = ImageDraw.Draw(img)
        left, top, right, bottom = draw.textbbox((0, 0), ch, font=font)
        draw.text(((size - (right - left)) / 2 - left, (size - (bottom - top)) / 2 - top), ch, fill=1, font=font)
        rows = ["".join(fill if img.getpixel((x, y)) else blank for x in range(size)) for y in range(size)]
        while rows and not rows[0].strip(blank):
            rows.pop(0)
        while rows and not rows[-1].strip(blank):
            rows.pop()
        blocks.append("\n".join(row.rstrip(blank) for row in rows))
    return "\n\n".join(blocks)


DOT_USAGE = "输入格式应为：点阵字 要转换的字 填充字"


def parse_dot_matrix(text: str) -> tuple[str, str] | None:
    parts = text.split()
    if len(parts) < 3:
        return None
    return parts[1], parts[2]


EMOJI_TRIGGER = re.compile(r"^合成")
