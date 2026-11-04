"""IP 地理位置与 Whois 查询（原 astrbot_plugin_ipquery），接口与输出格式保持不变。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib import parse

import httpx

from astrbot.api import logger

from .common import ASSET_DIR

IP_APIS = [
    {"name": "ip-api.com", "url": "http://ip-api.com/json/{}?fields=66846719", "type": "primary"},
    {"name": "snappytree镜像", "url": "https://is.snappytree.com/api/ipapi/{}", "type": "primary"},
    {"name": "ipapi.co备用", "url": "https://ipapi.co/{}/json/", "type": "backup"},
]
WHOIS_APIS = [
    {"name": "who-dat.as93.net", "url": "https://who-dat.as93.net/{}", "type": "primary"},
    {"name": "v2.xxapi.cn", "url": "https://v2.xxapi.cn/api/whois?domain={}", "type": "backup"},
]
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
HTTP_TIMEOUT = 8
USE_SYSTEM_PROXY = True
ALL_API_FAILED_MESSAGE = (
    "❌ 所有查询接口均已失效或达到频率上限。\n"
    "💡 建议：请稍后再试，或检查此查询目标是否已被 API 服务商限制。"
)
WHOIS_ALL_API_FAILED_MESSAGE = (
    "❌ 所有 WHOIS 查询接口均已失效或达到频率上限。\n"
    "💡 建议：请稍后再试，或检查此域名是否有效。"
)
IP_USAGE = (
    "❌ 请提供需要查询的 IPv4 地址、IPv6 地址或域名。\n\n"
    "用法：/查IP <IP地址或域名>\n"
    "例如：/查IP 8.8.8.8\n"
    "例如：/查IP 2400:3200::1\n"
    "例如：/查IP example.com"
)
WHOIS_USAGE = "❌ 请提供需要查询的域名信息。\n\n用法：/查域名 <域名>\n例如：/查域名 github.com"


def first_arg(message: str, command: str) -> str:
    match = re.search(rf"^{command}\s+(\S+)", message.strip(), re.IGNORECASE)
    return match.group(1) if match else ""


async def _get(url: str):
    async with httpx.AsyncClient(headers=HEADERS, timeout=HTTP_TIMEOUT, trust_env=USE_SYSTEM_PROXY) as client:
        return await client.get(url)


async def fetch_ip_info(ip: str) -> str:
    """查询 IP、IPv6 或域名的地理位置与网络类型。"""
    ip = (ip or "").strip()
    if not ip:
        return "❌ 请输入需要查询的 IP 地址、IPv6 地址或域名。"
    for api in IP_APIS:
        try:
            response = await _get(api["url"].format(parse.quote(ip, safe=".:")))
            if response.status_code in {403, 429}:
                logger.warning(f"[IP-Query] 源 {api['name']} 触发风控，正在尝试下一个...")
                continue
            response.raise_for_status()
            data = response.json()
            if api["type"] == "primary":
                if data.get("status") == "success" or "query" in data:
                    return format_primary_res(data)
            elif not data.get("error"):
                return format_backup_res(data)
        except Exception as exc:
            logger.error(f"[IP-Query] 源 {api['name']} 请求出错: {exc}")
    return ALL_API_FAILED_MESSAGE


def _is_primary_response(data: Any) -> bool:
    if not isinstance(data, dict) or data.get("error"):
        return False
    return any(
        field in data
        for field in ("query", "domain", "tld", "isRegistered", "registrar", "status",
                      "nameservers", "dates", "contacts", "meta")
    )


def _get_backup_data(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict) or data.get("error"):
        return None
    payload = data.get("data")
    if not isinstance(payload, dict):
        return None
    if "domain_status" not in payload and "domain_status" in data:
        payload = {**payload, "domain_status": data["domain_status"]}
    return payload


async def fetch_whois_info(domain: str) -> str:
    """使用主用、备用接口查询域名 WHOIS 信息。"""
    domain = (domain or "").strip()
    if not domain:
        return "❌ 请输入需要查询的域名。"
    encoded = parse.quote(domain, safe=".-_")
    for api in WHOIS_APIS:
        try:
            response = await _get(api["url"].format(encoded))
            if response.status_code in {403, 429}:
                logger.warning(f"[WHOIS-Query] 源 {api['name']} 触发风控，正在尝试下一个...")
                continue
            response.raise_for_status()
            data = response.json()
            if api["type"] == "primary" and _is_primary_response(data):
                return format_primary_whois(data)
            if api["type"] == "backup":
                backup = _get_backup_data(data)
                if backup is not None:
                    return format_backup_whois(backup)
        except Exception as exc:
            logger.error(f"[WHOIS-Query] 源 {api['name']} 请求出错: {exc}")
    return WHOIS_ALL_API_FAILED_MESSAGE


# ---------------------------------------------------------------- 原插件的输出格式

def format_primary_res(data: dict) -> str:
    """格式化主接口（ip-api.com / 镜像）返回的数据"""

    ip_tags = []

    if data.get("mobile"):
        ip_tags.append("这个IP可能是蜂窝移动网络")

    if data.get("proxy"):
        ip_tags.append("这个IP可能是代理/VPN IP")

    if data.get("hosting"):
        ip_tags.append("这个IP可能是数据中心/机房IP")

    tag_str = "\n".join(ip_tags) if ip_tags else "这个IP可能是家庭宽带或者其他IP"

    lat = data.get("lat", 0)
    lon = data.get("lon", 0)

    lat_dir = "北纬" if lat >= 0 else "南纬"
    lon_dir = "东经" if lon >= 0 else "西经"

    return (
        f"IP 查询结果 (主用源：ip-api.com):\n"
        f"------------------\n"
        f"查询 IP: {data.get('query')}\n"
        f"归属地: {data.get('country')} - {data.get('regionName')} - {data.get('city')}\n"
        f"国家/地区代码: {data.get('countryCode')}\n"
        f"大陆: {data.get('continent')}\n"
        f"运营商: {data.get('isp')}\n"
        f"时区: {data.get('timezone')}\n"
        f"组织: {data.get('org')}\n"
        f"AS号: {data.get('as')}\n"
        f"坐标: {lat_dir}{abs(lat)}°, {lon_dir}{abs(lon)}°\n"
        f"{tag_str}\n"
        f"------------------"
    )


def format_backup_res(data: dict) -> str:
    """格式化备用接口（ipapi.co）返回的数据"""

    latitude = data.get("latitude", 0)
    longitude = data.get("longitude", 0)

    lat_dir = "北纬" if latitude >= 0 else "南纬"
    lon_dir = "东经" if longitude >= 0 else "西经"

    return (
        f"IP 查询结果 (备用源：ipapi.co):\n"
        f"------------------\n"
        f"查询 IP: {data.get('ip')}\n"
        f"归属地: {data.get('country_name')} - {data.get('region')} - {data.get('city')}\n"
        f"国家/地区代码: {data.get('country_code')}\n"
        f"大陆: {data.get('continent_code')}\n"
        f"运营商: {data.get('isp')}\n"
        f"时区: {data.get('timezone')}\n"
        f"组织: {data.get('org')}\n"
        f"AS号: {data.get('asn')}\n"
        f"坐标: {lat_dir}{abs(latitude)}°, {lon_dir}{abs(longitude)}°\n"
        f"该源不支持查看IP类型\n"
        f"------------------"
    )


def _load_status_map() -> dict[str, str]:
    """读取 WHOIS 状态码汉化表。"""
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10 兼容
        try:
            import tomli as tomllib
        except ModuleNotFoundError:
            return {}

    try:
        with (ASSET_DIR / "whois_status.toml").open("rb") as file:
            mapping = tomllib.load(file)
    except (OSError, ValueError):
        return {}

    return {
        str(status): str(translation)
        for status, translation in mapping.items()
        if isinstance(translation, str)
    }


STATUS_MAP = _load_status_map()


def _display(value: Any) -> str:
    """将空值显示为字面量 null，避免 WHOIS 结果中出现 None。"""
    if value is None:
        return "null"
    if isinstance(value, str):
        return value if value.strip() else "null"
    if isinstance(value, (list, tuple)):
        if not value:
            return "null"
        return ", ".join(_display(entry) for entry in value)
    return str(value)


def _field(data: dict[str, Any], key: str) -> str:
    return _display(data.get(key))


def _format_date(value: Any) -> str:
    """将 WHOIS 日期格式化为 yyyy年 MM月dd日 HH:mm:ss。"""
    displayed = _display(value)
    if displayed == "null":
        return displayed

    date_text = displayed
    if date_text.endswith("Z"):
        date_text = f"{date_text[:-1]}+00:00"

    try:
        parsed = datetime.fromisoformat(date_text)
        return parsed.strftime("%Y年 %m月%d日 %H:%M:%S")
    except ValueError:
        # 对无法解析的异常日期，至少按要求去除末尾的 Z。
        return displayed[:-1] if displayed.endswith("Z") else displayed


def _format_status(value: Any) -> str:
    def translate(status: Any) -> str:
        displayed = _display(status)
        return STATUS_MAP.get(displayed, displayed)

    if isinstance(value, (list, tuple)):
        if not value:
            return "null"
        return ", ".join(translate(status) for status in value)
    if isinstance(value, str):
        displayed = _display(value)
        if displayed == "null":
            return displayed
        return ", ".join(translate(status.strip()) for status in displayed.split(","))
    return _display(value)


def format_primary_whois(data: dict[str, Any]) -> str:
    """格式化 who-dat.as93.net 返回的 WHOIS 数据。"""
    registrar = data.get("registrar") or {}
    dates = data.get("dates") or {}
    contacts = data.get("contacts") or {}
    registrant = contacts.get("registrant") or {}
    meta = data.get("meta") or {}
    nameservers = data.get("nameservers") or []

    nameserver_lines = []
    for nameserver in nameservers:
        nameserver = nameserver if isinstance(nameserver, dict) else {}
        nameserver_lines.extend(
            (
                f"  名称: {_field(nameserver, 'name')}",
                f"  IPv4: {_field(nameserver, 'ipv4')}",
                f"  IPv6: {_field(nameserver, 'ipv6')}",
            )
        )
    nameserver_text = "\n".join(nameserver_lines) if nameserver_lines else "  null"

    return (
        "WHOIS 查询结果 (主用源：who-dat.as93.net):\n"
        "------------------\n"
        f"查询: {_field(data, 'query')}\n"
        f"域名: {_field(data, 'domain')}\n"
        f"顶级域名: {_field(data, 'tld')}\n"
        f"是否已注册: {_field(data, 'isRegistered')}\n"
        "\n"
        "注册商:\n"
        f"  名称: {_field(registrar, 'name')}\n"
        f"  IANA ID: {_field(registrar, 'ianaId')}\n"
        f"  URL: {_field(registrar, 'url')}\n"
        f"  WHOIS 服务器: {_field(registrar, 'whoisServer')}\n"
        f"状态: {_format_status(data.get('status'))}\n"
        "\n"
        "名称服务器:\n"
        f"{nameserver_text}\n"
        "\n"
        "日期:\n"
        f"  创建时间: {_format_date(dates.get('created'))}\n"
        f"  更新时间: {_format_date(dates.get('updated'))}\n"
        f"  到期时间: {_format_date(dates.get('expires'))}\n"
        "\n"
        "注册人:\n"
        f"  姓名: {_field(registrant, 'name')}\n"
        f"  组织: {_field(registrant, 'organization')}\n"
        f"  邮箱: {_field(registrant, 'email')}\n"
        f"  电话: {_field(registrant, 'phone')}\n"
        f"  注册商隐私保护: {_field(registrant, 'redacted')}\n"
        "\n"
        "元数据:\n"
        f"  数据来源协议: {_field(meta, 'source')}\n"
        f"  服务器: {_field(meta, 'server')}\n"
        f"  是否缓存: {_field(meta, 'cached')}\n"
        "------------------"
    )


def format_backup_whois(data: dict[str, Any]) -> str:
    """格式化 v2.xxapi.cn 返回的 WHOIS 数据。"""
    return (
        "WHOIS 查询结果 (备用源：v2.xxapi.cn):\n"
        "------------------\n"
        f"域名: {_field(data, 'domain_name')}\n"
        f"状态: {_format_status(data.get('domain_status'))}\n"
        f"注册时间: {_format_date(data.get('registration_time'))}\n"
        f"到期时间: {_format_date(data.get('expiration_time'))}\n"
        f"注册人: {_field(data, 'registrant')}\n"
        f"注册人邮箱: {_field(data, 'registrant_contact_email')}\n"
        f"注册商 URL: {_field(data, 'registrar_url')}\n"
        f"DNS 服务器: {_field(data, 'dns_serve')}\n"
        "------------------"
    )
