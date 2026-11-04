"""百度翻译（原 astrbot_plugin_Translation）。"""

from __future__ import annotations

import hashlib
import json
import random

from .common import fetch

API_URL = "https://fanyi-api.baidu.com/api/trans/vip/translate"

LANGUAGE_CODE_MAP = {
    "中文": "zh", "英语": "en", "粤语": "yue", "文言文": "wyw", "日语": "jp",
    "韩语": "kor", "法语": "fra", "西班牙语": "spa", "泰语": "th", "阿拉伯语": "ara",
    "俄语": "ru", "葡萄牙语": "pt", "德语": "de", "意大利语": "it", "希腊语": "el",
    "荷兰语": "nl", "波兰语": "pl", "保加利亚语": "bul", "爱沙尼亚语": "est", "丹麦语": "dan",
    "芬兰语": "fin", "捷克语": "cs", "罗马尼亚语": "rom", "斯洛文尼亚语": "slo", "瑞典语": "swe",
    "匈牙利语": "hu", "繁体中文": "cht", "越南语": "vie",
}

USAGE = "格式错误，请使用 /fy <目标语种> <翻译的内容> 或 /fy help 查看帮助。"


def help_text() -> str:
    names = list(LANGUAGE_CODE_MAP)
    lines = ["翻译插件使用说明：", "/fy <目标语种> <翻译的内容>", "", "目标语种支持："]
    for i in range(0, len(names), 3):
        lines.append("  ".join(names[i:i + 3]))
    return "\n".join(lines)


async def translate(appid: str, secret: str, target_name: str, text: str) -> str:
    if not appid or not secret:
        return "❌ 翻译功能未配置！请管理员在插件配置「百度翻译设置」中填写 APP ID 和密钥。"
    target = LANGUAGE_CODE_MAP.get(target_name)
    if not target:
        return f"不支持的目标语种: {target_name}"
    salt = str(random.randint(10000, 99999))
    sign = hashlib.md5(f"{appid}{text}{salt}{secret}".encode()).hexdigest()
    params = {"q": text, "from": "auto", "to": target, "appid": appid, "salt": salt, "sign": sign}
    status, _, body = await fetch(API_URL, params=params, seconds=15)
    try:
        result = json.loads(body.decode("utf-8", errors="ignore"))
    except ValueError:
        return f"翻译失败: HTTP {status}"
    if "trans_result" in result:
        return "翻译结果: " + "\n".join(item.get("dst", "") for item in result["trans_result"])
    return f"翻译失败: {result.get('error_msg', '未知错误')}"
