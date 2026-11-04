"""Safebooru 图片（原 astrbot_plugin_safebooru）。"""

from __future__ import annotations

import difflib
import json
from functools import lru_cache
from pathlib import Path

from .common import ASSET_DIR, fetch

POSTS_URL = "https://safebooru.org/index.php"
RANDOM_URL = "https://safebooru.org/index.php?page=post&s=random"
RANDOM_XPATH = "/html/body/div[5]/div/div[2]/div[1]/div[2]/div[1]/img"


@lru_cache(maxsize=1)
def tag_mapping() -> dict[str, str]:
    """中文标签 -> safebooru 标签。"""
    data = json.loads((ASSET_DIR / "safebooru_tags.json").read_text(encoding="utf-8"))
    data.pop("right_tag_cn", None)
    return data


def resolve_tag(tag: str) -> str:
    """英文标签原样使用；中文按映射表模糊匹配，匹配不到就原样使用。"""
    mapping = tag_mapping()
    if tag in mapping.values():
        return tag
    if tag in mapping:
        return mapping[tag]
    matches = difflib.get_close_matches(tag, list(mapping), n=1, cutoff=0.1)
    return mapping[matches[0]] if matches else tag


class Safebooru:
    def __init__(self, data_dir: Path):
        self.usage_file = Path(data_dir) / "safebooru_usage.json"
        try:
            self.usage = json.loads(self.usage_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.usage = {}

    def _save(self) -> None:
        try:
            self.usage_file.write_text(json.dumps(self.usage, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    async def pick(self, tag: str, limit: int) -> dict:
        """按使用次数轮换同一标签下的图片，返回 post 字典。"""
        query = resolve_tag(tag)
        params = {"page": "dapi", "s": "post", "q": "index", "tags": query, "limit": int(limit or 100), "json": 1}
        status, _, body = await fetch(POSTS_URL, params=params, seconds=20)
        if status != 200:
            raise RuntimeError("无法获取图片数据，请稍后重试。")
        try:
            posts = json.loads(body.decode("utf-8", errors="ignore") or "[]")
        except ValueError:
            posts = []
        if not posts:
            raise RuntimeError("未找到相关图片。")
        count = int(self.usage.get(query, 0))
        post = posts[count % len(posts)]
        if not post.get("file_url"):
            raise RuntimeError("未获取到图片链接。")
        self.usage[query] = count + 1
        self._save()
        return post

    @staticmethod
    async def random_image() -> tuple[str, str]:
        """返回 (图片链接, 标签)。"""
        import lxml.html

        status, _, body = await fetch(RANDOM_URL, seconds=20)
        if status != 200:
            raise RuntimeError("请求随机图片失败，请稍后重试。")
        doc = lxml.html.fromstring(body.decode("utf-8", errors="ignore"))
        elements = doc.xpath(RANDOM_XPATH) or doc.xpath('//img[@id="image"]')
        if not elements:
            raise RuntimeError("未能找到随机图片。")
        img = elements[0]
        return img.get("src"), img.get("alt", "未知标签")
