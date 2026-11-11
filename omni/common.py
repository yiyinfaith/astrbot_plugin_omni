"""各功能共用的小工具：配置读取、HTTP 请求、取图、临时文件、等待发图。"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any

import aiohttp

PLUGIN_NAME = "astrbot_plugin_omni"
PLUGIN_DIR = Path(__file__).resolve().parent.parent
ASSET_DIR = Path(__file__).resolve().parent / "data"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def section(config: Any, name: str) -> dict:
    """读取配置里的分组（object），缺失时返回空字典。"""
    value = config.get(name) if config is not None else None
    return value if isinstance(value, dict) else {}


def command_rest(event: Any) -> str:
    """去掉指令名后的参数文本，例如 `fy 英语 你好` -> `英语 你好`。"""
    parts = (getattr(event, "message_str", "") or "").strip().split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def sender_key(event: Any) -> str:
    return f"{event.unified_msg_origin}:{event.get_sender_id()}"


def timeout(seconds: float) -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(total=max(1.0, float(seconds or 10)))


async def fetch(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    seconds: float = 15,
) -> tuple[int, str, bytes]:
    """GET 请求，返回 (状态码, Content-Type, 内容)。"""
    merged = {"User-Agent": UA}
    merged.update(headers or {})
    async with aiohttp.ClientSession(timeout=timeout(seconds), headers=merged) as session:
        async with session.get(url, params=params, allow_redirects=True) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), await resp.read()


def find_image_url(data: Any) -> str:
    """在接口返回的 JSON 里找图片链接（兼容 text/url/data/imgurl 等常见字段）。"""
    if isinstance(data, str):
        return data.strip() if data.strip().startswith(("http://", "https://")) else ""
    if isinstance(data, list):
        for item in data:
            found = find_image_url(item)
            if found:
                return found
        return ""
    if isinstance(data, dict):
        for key in ("url", "imgurl", "img", "image", "pic", "text", "data", "result"):
            if key in data:
                found = find_image_url(data[key])
                if found:
                    return found
    return ""


async def fetch_image_from_api(url: str, *, params: dict | None = None, seconds: float = 15) -> bytes:
    """调用随机图片类接口：直接返回图片或返回带图片链接的 JSON 都支持。"""
    import json

    # 有的接口挂在 CDN 后面，同一个地址的结果会被缓存一整天，每次拿到同一张图；带一个每次不同的参数绕开缓存
    params = {**(params or {}), "_": uuid.uuid4().hex}
    status, ctype, body = await fetch(url, params=params, seconds=seconds)
    if status != 200:
        raise RuntimeError(f"接口返回 HTTP {status}")
    if ctype.startswith("image/") or body[:4] in (b"\x89PNG", b"RIFF", b"GIF8") or body[:2] == b"\xff\xd8":
        return body
    try:
        data = json.loads(body.decode("utf-8", errors="ignore"))
    except ValueError:
        data = body.decode("utf-8", errors="ignore")
    image_url = find_image_url(data)
    if not image_url:
        raise RuntimeError("接口没有返回图片")
    status, _, image = await fetch(image_url, seconds=seconds)
    if status != 200 or not image:
        raise RuntimeError(f"图片下载失败 HTTP {status}")
    return image


class TempFiles:
    """插件数据目录下的临时文件，按存活时间清理。"""

    def __init__(self, root: Path, max_age: float = 3600):
        self.root = Path(root)
        self.max_age = max_age
        self.root.mkdir(parents=True, exist_ok=True)

    def new_path(self, suffix: str = ".png", prefix: str = "omni") -> str:
        self.cleanup()
        return str(self.root / f"{prefix}_{uuid.uuid4().hex[:12]}{suffix}")

    def write(self, data: bytes, suffix: str = ".png", prefix: str = "omni") -> str:
        path = self.new_path(suffix, prefix)
        Path(path).write_bytes(data)
        return path

    def cleanup(self) -> None:
        now = time.time()
        for item in self.root.glob("*"):
            try:
                if item.is_file() and now - item.stat().st_mtime > self.max_age:
                    item.unlink()
            except OSError:
                pass


def image_suffix(data: bytes) -> str:
    if data[:4] == b"\x89PNG":
        return ".png"
    if data[:4] == b"RIFF":
        return ".webp"
    if data[:4] == b"GIF8":
        return ".gif"
    return ".jpg"


def message_images(event: Any) -> list:
    """消息里的图片组件：先取引用消息里的，再取本条消息里的。"""
    import astrbot.api.message_components as Comp

    quoted, direct = [], []
    for seg in event.get_messages() or []:
        if isinstance(seg, Comp.Image):
            direct.append(seg)
        elif isinstance(seg, Comp.Reply):
            for inner in getattr(seg, "chain", None) or []:
                if isinstance(inner, Comp.Image):
                    quoted.append(inner)
    return quoted + direct


async def image_bytes(image: Any) -> bytes:
    path = await image.convert_to_file_path()
    if not path or not os.path.exists(path):
        raise RuntimeError("图片下载失败")
    return Path(path).read_bytes()


class ImageWaiter:
    """记录“请发送图片”的等待状态，按会话 + 用户区分，互不干扰。"""

    def __init__(self):
        self._pending: dict[str, tuple[float, float]] = {}

    def start(self, event: Any, seconds: float) -> None:
        seconds = max(5.0, float(seconds or 60))
        self._pending[sender_key(event)] = (time.monotonic() + seconds, seconds)

    def cancel(self, event: Any) -> None:
        self._pending.pop(sender_key(event), None)

    def state(self, event: Any) -> str:
        """返回 none / waiting / expired；过期的状态会被清除。

        超时后再等一个同样长的时间段内发图才提示“超时了”，更晚发的图当作普通消息，不再打扰。
        """
        key = sender_key(event)
        entry = self._pending.get(key)
        if entry is None:
            return "none"
        deadline, seconds = entry
        now = time.monotonic()
        if now <= deadline:
            return "waiting"
        self._pending.pop(key, None)
        return "expired" if now <= deadline + seconds else "none"
