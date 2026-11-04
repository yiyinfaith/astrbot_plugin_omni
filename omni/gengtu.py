"""梗图抽象猜词（原 astrbot_gengtu），使用柠柚 API。"""

from __future__ import annotations

import json

from astrbot.api import logger

from .common import fetch

DEFAULT_URL = "https://api.nycnm.cn/api/v2/gengtu"
IMAGE_HEADERS = {
    "Referer": "https://www.xiaohongshu.com/",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}
GUIDE_TEXT = "📝 使用 /答案 你的答案 作答\n💡 查看提示：/提示 或 /hint"
NO_QUESTION = "ℹ️ 当前没有待作答的题目，请先使用 /梗图 获取题目"
NO_KEY = "❌ 梗图猜词需要柠柚 API Key，请管理员在插件配置「柠柚 API」中填写"
HELP_TEXT = """🎯 梗图抽象猜词使用说明

🖼️ 获取题目：
• /梗图 或 /gengtu

📝 提交答案：
• /答案 你的答案
  例如：/答案 六六大顺

💡 获取提示：
• /提示 或 /hint
  显示当前题目的正确答案，但仍需要正确回答才能完成题目

💡 说明：
• 发送图片后，会为你记录题目编号（每人独立）
• 回答错误时不会显示正确答案，需要使用 /提示 命令查看
• 如需新的题目，直接再次输入 /梗图"""


class Gengtu:
    def __init__(self):
        self.pending: dict[str, int] = {}

    async def _call(self, url: str, params: dict, seconds: float) -> dict | None:
        status, _, body = await fetch(url or DEFAULT_URL, params=params, seconds=seconds)
        if status != 200:
            logger.error(f"[梗图] 接口返回 HTTP {status}")
            return None
        try:
            data = json.loads(body.decode("utf-8", errors="ignore"))
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    async def fetch_question(self, url: str, key: str, seconds: float) -> tuple[int, str] | None:
        data = await self._call(url, {"apikey": key}, seconds)
        question = ((data or {}).get("data") or {}).get("question") or {}
        qid, image = question.get("id"), question.get("image")
        if isinstance(qid, int) and isinstance(image, str) and image:
            return qid, image
        return None

    async def download_image(self, image_url: str, seconds: float) -> bytes | None:
        try:
            status, _, body = await fetch(image_url, headers=IMAGE_HEADERS, seconds=seconds)
        except Exception as exc:
            logger.error(f"[梗图] 图片下载失败: {exc}")
            return None
        return body if status == 200 and body else None

    async def verify(self, url: str, key: str, qid: int, answer: str, seconds: float):
        """返回 (提示消息, 是否正确 or None, 正确答案 or None)。"""
        data = await self._call(url, {"check": qid, "answer": answer, "apikey": key}, seconds)
        if data is None:
            return "❌ 校验失败，请稍后重试", None, None
        message = str(data.get("message", "") or "")
        pdata = data.get("data") if isinstance(data.get("data"), dict) else {}
        correct = pdata.get("correct") if isinstance(pdata.get("correct"), bool) else None
        answer_text = pdata.get("correct_answer") if isinstance(pdata.get("correct_answer"), str) else None
        if not message:
            message = "✅ 回答正确！" if correct else "❌ 回答不正确！"
        return message, correct, answer_text

    async def hint(self, url: str, key: str, qid: int, seconds: float) -> str | None:
        data = await self._call(url, {"check": qid, "answer": "", "apikey": key}, seconds)
        pdata = (data or {}).get("data")
        answer = pdata.get("correct_answer") if isinstance(pdata, dict) else None
        return answer if isinstance(answer, str) and answer else None
