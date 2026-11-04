"""AICU B 站用户评论画像（原 astrbot_plugin_aicu）。"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from astrbot.api import logger

from .browser import SharedBrowser, block_private_requests

BILI_API_URL = "https://worker.aicu.cc/api/bili/space"
MARK_API_URL = "https://api.aicu.cc/api/v3/user/getusermark"
REPLY_API_URL = "https://api.aicu.cc/api/v3/search/getreply"
REPLY_PAGE_SIZE = 100
DEFAULT_AVATAR_URL = "https://i0.hdslb.com/bfs/face/member/noface.jpg"
TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "aicu.html"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "accept-language": "zh-CN,zh;q=0.9",
    "cache-control": "no-cache",
    "origin": "https://www.aicu.cc",
    "referer": "https://www.aicu.cc/",
    "pragma": "no-cache",
    "priority": "u=1, i",
    "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
}


async def _request(url: str, params: dict, cookie: str):
    from curl_cffi.requests import AsyncSession

    headers = dict(HEADERS)
    if cookie:
        headers["cookie"] = cookie
    async with AsyncSession() as session:
        try:
            response = await session.get(url, params=params, headers=headers, timeout=20)
            if response.status_code != 200:
                logger.warning(f"[AICU] 请求返回 {response.status_code} | URL: {url}")
                return None
            return await asyncio.to_thread(response.json)
        except Exception as exc:
            logger.error(f"[AICU] 网络请求异常: {exc}")
            return None


async def fetch_all(uid: str, cookie: str):
    reply_params = {"uid": uid, "pn": "1", "ps": str(REPLY_PAGE_SIZE), "mode": "0", "keyword": ""}
    task_bili = _request(BILI_API_URL, {"mid": uid}, cookie)
    task_mark = _request(MARK_API_URL, {"uid": uid}, cookie)
    reply = await _request(REPLY_API_URL, reply_params, cookie)
    if (not reply or not reply.get("data")) and cookie:
        logger.info("[AICU] 评论获取失败，尝试不带 Cookie 重试...")
        reply = await _request(REPLY_API_URL, reply_params, "")
    bili, mark = await asyncio.gather(task_bili, task_mark)
    return bili, mark, reply


def parse_profile(bili_raw, uid: str) -> dict:
    profile = {"name": f"UID:{uid}", "avatar": DEFAULT_AVATAR_URL, "sign": "", "level": 0,
               "vip_label": "", "fans": 0, "following": 0}
    if not bili_raw or bili_raw.get("code") != 0:
        return profile
    card = (bili_raw.get("data") or {}).get("card") or {}
    if card:
        profile.update(
            name=card.get("name", uid), avatar=card.get("face", profile["avatar"]), sign=card.get("sign", ""),
            fans=card.get("fans", 0), following=card.get("friend", 0),
            level=(card.get("level_info") or {}).get("current_level", 0),
        )
        label = ((card.get("vip") or {}).get("label") or {}).get("text")
        if label:
            profile["vip_label"] = label
    return profile


def parse_device(mark_raw, has_cookie: bool) -> tuple[str, list]:
    device_name, history = "未知设备", []
    if mark_raw and mark_raw.get("code") == 0:
        data = mark_raw.get("data") or {}
        devices = data.get("device") or []
        if devices:
            device_name = devices[0].get("name") or devices[0].get("type")
        history = data.get("hname") or []
    elif not has_cookie:
        device_name = "需配置Cookie"
    return device_name, history


def parse_replies(reply_raw) -> dict:
    replies = []
    if reply_raw and reply_raw.get("code") == 0:
        block = reply_raw.get("data") or {}
        if "replies" not in block and isinstance(block.get("data"), dict):
            block = block["data"]
        replies = block.get("replies") or []
    formatted, hours, lengths = [], [], []
    for i, r in enumerate(replies):
        ts = r.get("time", 0)
        dt = datetime.fromtimestamp(ts)
        msg = r.get("message", "")
        hours.append(dt.strftime("%H"))
        lengths.append(len(msg))
        formatted.append({"index": i + 1, "message": msg, "readable_time": dt.strftime("%Y-%m-%d %H:%M"),
                          "rank": r.get("rank", 0), "timestamp": ts})
    hour_counts = Counter(hours)
    top = hour_counts.most_common(1)
    return {
        "list": formatted,
        "count": len(formatted),
        "stats": {
            "active_hour": top[0][0] if top else "N/A",
            "hour_dist": dict(hour_counts.most_common(5)),
            "max_hour_count": max(hour_counts.values()) if hour_counts else 0,
            "avg_length": round(sum(lengths) / len(lengths), 1) if lengths else 0,
        },
    }


async def analyze(browser: SharedBrowser, uid: str, cookie: str, output_dir: Path) -> str | None:
    """返回生成的图片路径；数据完全拿不到时返回 None。"""
    import jinja2

    bili_raw, mark_raw, reply_raw = await fetch_all(uid, cookie)
    if not bili_raw and not reply_raw:
        return None
    profile = parse_profile(bili_raw, uid)
    device_name, history_names = parse_device(mark_raw, bool(cookie))
    replies = parse_replies(reply_raw)
    stats = replies["stats"]
    # 昵称、签名、评论都是 B 站用户写的，必须转义后再放进页面
    template = jinja2.Environment(autoescape=True).from_string(TEMPLATE_PATH.read_text(encoding="utf-8"))
    html = template.render(
        uid=uid, profile=profile, device_name=device_name, history_names=history_names[:10],
        total_count=replies["count"], avg_length=stats["avg_length"], active_hour=stats["active_hour"],
        hour_dist=stats["hour_dist"], max_hour_count=stats["max_hour_count"], replies=replies["list"],
        generate_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"aicu_{uid}_{int(time.time())}.png"
    instance = await browser.get()
    page = await instance.new_page(viewport={"width": 600, "height": 800}, device_scale_factor=2)
    try:
        await block_private_requests(page)
        await page.set_content(html, wait_until="networkidle")
        try:
            await page.locator(".container").screenshot(path=str(path))
        except Exception as exc:
            logger.warning(f"[AICU] 局部截图失败，尝试全页截图: {exc}")
            await page.screenshot(path=str(path), full_page=True)
    finally:
        await page.close()
    return str(path)
