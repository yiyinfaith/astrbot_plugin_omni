"""卢恩符文占卜（原 astrbot_plugin_runes），提示词与原插件一致。"""

from __future__ import annotations

import json
import random
from functools import lru_cache

from .common import ASSET_DIR

METHODS = ("单符", "三符", "五符", "九符")
USAGE = "请指定占卜方式: 单符/三符/五符/九符/符文表\n示例: 卢恩占卜 单符 我今天运势如何"
INVALID = "无效的占卜方式，请使用: 单符/三符/五符/九符/符文表"
SYSTEM_BASE = "你是一个专业的卢恩符文占卜师，精通北欧神话和符文象征意义。"

LAYOUTS = {
    "三符": {
        "title": "三符文占卜结果",
        "positions": ["过去", "现在", "未来"],
        "ask": "请根据这三个符文的正逆位关系，分析过去、现在和未来的发展脉络，",
        "history": "\n\n请包含一些关于这些符文的历史背景和北欧神话中的相关故事，特别是它们如何相互作用形成连贯的叙事。",
        "system": "请根据用户抽到的三个符文和问题，分析过去、现在和未来的发展脉络，提供专业且有连贯性的占卜解读。",
    },
    "五符": {
        "title": "五符文十字占卜结果",
        "positions": ["中心(现状)", "上方(挑战)", "下方(建议)", "左方(助力)", "右方(结果)"],
        "ask": "请根据这五个符文的正逆位关系，分析现状、挑战、建议、助力和可能的结果，",
        "history": "\n\n请包含一些关于这些符文的历史背景和北欧神话中的相关故事，特别是它们如何在这个十字布局中相互作用。",
        "system": "请根据用户抽到的五个符文和问题，分析现状、挑战、建议、助力和可能的结果，提供专业且有洞察力的占卜解读。",
    },
    "九符": {
        "title": "九符文世界之树占卜结果",
        "positions": [
            "阿斯加德(神之国/最终结果)", "华纳海姆(华纳神族/情感关系)", "亚尔夫海姆(光之精灵/精神指引)",
            "米德加尔特(人类世界/现状)", "约顿海姆(巨人国度/挑战)", "瓦特阿尔海姆(黑暗精灵/物质基础)",
            "尼福尔海姆(雾之国/潜意识)", "穆斯贝尔海姆(火之国/创造力)", "赫尔海姆(死之国/隐藏力量)",
        ],
        "ask": "请根据这九个符文在世界之树九个国度中的位置和正逆位关系，",
        "history": "\n\n请包含一些关于这些符文的历史背景和北欧神话中的相关故事，特别是它们如何在世界之树的九个国度中相互作用。",
        "system": "请根据用户抽到的九个符文在世界之树九个国度中的位置和问题，提供全面且深刻的占卜解读。",
    },
}


@lru_cache(maxsize=1)
def runes() -> tuple[dict, ...]:
    data = json.loads((ASSET_DIR / "runes.json").read_text(encoding="utf-8"))
    return tuple(data.get("runes", []))


def _depth_word(depth: str) -> str:
    return "简要" if depth == "简洁" else "详细" if depth == "详细" else "深入"


def _label(rune: dict, reversed_: bool) -> str:
    return f"{rune['symbol']} {rune['name']} ({'逆位' if reversed_ else '正位'})"


def table_request() -> tuple[str, str]:
    infos = [
        f"符文: {r['symbol']} {r['name']} ({r['english_name']})\n"
        f"含义: {r['meaning']}\n逆位: {r['reversed_meaning']}\n"
        f"关键词: {', '.join(r['keywords'])}\n类别: {r['category']}\n"
        for r in runes()
    ]
    prompt = (
        "请将以下卢恩符文信息整理成清晰易读的格式:\n\n" + "\n\n".join(infos)
        + "\n\n请按类别分组展示符文信息，每组之间用分隔线隔开。"
    )
    return prompt, "你是一个专业的卢恩符文占卜师，请将符文信息整理成清晰易读的格式，按类别分组展示。"


def divine(method: str, question: str, depth: str, include_history: bool, rng=random) -> tuple[str, str, str]:
    """返回 (先发给用户的抽牌结果, LLM 提示词, system prompt)。"""
    if method == "单符":
        rune = rng.sample(runes(), 1)[0]
        rev = rng.choice([True, False])
        display = f"您抽到的符文是: {_label(rune, rev)}\n正在为您解读..."
        prompt = (
            f"问题: {question}\n\n抽到的符文: {_label(rune, rev)}\n\n"
            f"请根据{'逆位' if rev else '正位'}符文的含义，为这个问题提供{_depth_word(depth)}的解读。"
        )
        if include_history:
            prompt += f"\n\n请包含一些关于{rune['name']}符文的历史背景和北欧神话中的相关故事。"
        system = SYSTEM_BASE + "请根据用户抽到的符文和问题，提供通俗易懂质朴且有文化内涵的占卜解读。"
        return display, prompt, system

    layout = LAYOUTS[method]
    positions = layout["positions"]
    drawn = rng.sample(runes(), len(positions))
    revs = [rng.choice([True, False]) for _ in positions]
    if method == "九符":
        head = "您在世界之树九个国度抽到的符文是:\n"
        lines = [f"{i + 1}. {positions[i]}: {_label(drawn[i], revs[i])}" for i in range(len(positions))]
    else:
        head = "您抽到的符文是:\n"
        lines = [f"{positions[i]}: {_label(drawn[i], revs[i])}" for i in range(len(positions))]
    display = head + "\n".join(lines) + "\n\n正在为您解读..."
    numbered = [f"{i + 1}. {positions[i]}: {_label(drawn[i], revs[i])}" for i in range(len(positions))]
    prompt = (
        f"问题: {question}\n\n{layout['title']}:\n" + "\n".join(numbered) + "\n\n"
        + layout["ask"] + f"提供{_depth_word(depth)}的解读。"
    )
    if include_history:
        prompt += layout["history"]
    return display, prompt, SYSTEM_BASE + layout["system"]
