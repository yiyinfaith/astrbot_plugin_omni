"""图片标签识别（原 astrbot_plugin_tagger），调用 WD Tagger 类 Gradio 应用。"""

from __future__ import annotations

import json
import uuid

import aiohttp

DEFAULT_API_URL = "https://smilingwolf-wd-tagger.hf.space/gradio_api"
DEFAULT_MODEL = "SmilingWolf/wd-swinv2-tagger-v3"


def format_result(result_data: list) -> str:
    if len(result_data) < 3:
        return "❌ 未找到标签数据"
    general, characters = result_data[0], result_data[2]
    lines = [f"标签：\n{general}"]
    if isinstance(characters, dict) and characters.get("confidences"):
        found = [
            f"{c['label']} ({c['confidence'] * 100:.1f}%)"
            for c in sorted(characters["confidences"], key=lambda c: c.get("confidence") or 0, reverse=True)
            if (c.get("confidence") or 0) > 0.5
        ]
        if found:
            lines.append("角色：\n" + "\n".join(found))
    return "\n".join(lines)


async def analyze(
    image: bytes,
    api_url: str = DEFAULT_API_URL,
    model: str = DEFAULT_MODEL,
    general_threshold: float = 0.35,
    character_threshold: float = 0.85,
    seconds: float = 120,
) -> str:
    """上传图片 → 调用 /call/predict → 读取 SSE 结果。"""
    base = (api_url or DEFAULT_API_URL).rstrip("/")
    timeout = aiohttp.ClientTimeout(total=seconds)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        form = aiohttp.FormData()
        form.add_field("files", image, filename="image.png")
        async with session.post(f"{base}/upload", data=form) as resp:
            if resp.status != 200:
                raise RuntimeError(f"上传图片失败: HTTP {resp.status}")
            path = (await resp.json(content_type=None))[0]
        payload = {
            "data": [
                {"path": path, "url": f"{base}/file={path}", "meta": {"_type": "gradio.FileData"}},
                model or DEFAULT_MODEL,
                float(general_threshold),
                False,
                float(character_threshold),
                False,
            ],
            "session_hash": uuid.uuid4().hex[:11],
        }
        async with session.post(f"{base}/call/predict", json=payload) as resp:
            if resp.status != 200:
                raise RuntimeError(f"提交识别失败: HTTP {resp.status} {(await resp.text())[:200]}")
            event_id = (await resp.json(content_type=None)).get("event_id")
        if not event_id:
            raise RuntimeError("提交识别失败：没有返回任务 ID")
        async with session.get(f"{base}/call/predict/{event_id}") as resp:
            event = ""
            async for raw in resp.content:
                line = raw.decode("utf-8", errors="ignore").strip()
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data = line[5:].strip()
                    if event == "complete":
                        return format_result(json.loads(data))
                    if event == "error":
                        raise RuntimeError(f"识别失败：{data[:200] if data and data != 'null' else '模型服务报错'}")
    raise RuntimeError("未收到有效的分析结果")
