"""Markdown 转图片和 AICU 共用的 Playwright 浏览器，首次使用时启动，插件卸载时关闭。"""

from __future__ import annotations

import asyncio
import ipaddress
import sys
from urllib.parse import urlsplit

from astrbot.api import logger


def is_public_url(url: str) -> bool:
    """只允许访问公网 http(s) 资源和内嵌数据，挡住 localhost、内网地址和本地文件。"""
    parts = urlsplit(url)
    if parts.scheme in ("data", "blob", "about"):
        return True
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").strip("[]").lower()
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    return ip.is_global


async def block_private_requests(target) -> None:
    """给 Playwright 的 page 或 context 加上请求过滤，渲染的内容来自用户或 LLM，不能让它访问内网。"""

    async def handle(route):
        if is_public_url(route.request.url):
            await route.continue_()
        else:
            await route.abort()

    await target.route("**/*", handle)


class SharedBrowser:
    def __init__(self):
        self._playwright = None
        self._browser = None
        self._lock = asyncio.Lock()
        self._install_tried = False

    async def _launch(self):
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        try:
            try:
                self._browser = await self._playwright.chromium.launch(headless=True)
            except Exception:
                self._browser = await self._playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        except Exception:
            await self._playwright.stop()
            self._playwright = None
            raise

    async def _install_chromium(self) -> None:
        self._install_tried = True
        logger.info("[综合插件] 未找到 Chromium，正在执行 playwright install chromium ...")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "playwright", "install", "chromium",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            logger.error(f"[综合插件] 安装 Chromium 失败：{err.decode('utf-8', errors='ignore')[-500:]}")

    async def get(self):
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            await self.close_unlocked()
            try:
                await self._launch()
            except Exception as exc:
                if self._install_tried or "Executable doesn't exist" not in str(exc):
                    raise
                await self._install_chromium()
                await self._launch()
            return self._browser

    async def close_unlocked(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    async def close(self) -> None:
        async with self._lock:
            await self.close_unlocked()
