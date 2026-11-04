"""天气查询（原 astrbot_plugin_weather），基于心知天气免费 API。"""

from __future__ import annotations

import json
from typing import Awaitable, Callable

from astrbot.api import logger

from .common import fetch

NOW_URL = "https://api.seniverse.com/v3/weather/now.json"
DAILY_URL = "https://api.seniverse.com/v3/weather/daily.json"
SUGGESTION_URL = "https://api.seniverse.com/v3/life/suggestion.json"
SUGGESTION_NAMES = {
    "dressing": "穿衣", "umbrella": "雨伞", "car_washing": "洗车",
    "flu": "感冒", "sport": "运动", "uv": "紫外线",
}
NO_KEY = "未配置心知天气 API Key，无法查询天气。请在插件配置「天气」中填写后再试。"


async def _results(url: str, params: dict) -> list | None:
    try:
        status, _, body = await fetch(url, params=params, seconds=10)
    except Exception as exc:
        logger.error(f"[天气] 请求失败: {exc}")
        return None
    if status != 200:
        logger.error(f"[天气] {url} 返回 HTTP {status}")
        return None
    try:
        data = json.loads(body.decode("utf-8", errors="ignore"))
    except ValueError:
        return None
    results = data.get("results") if isinstance(data, dict) else None
    return results or None


async def current(api_key: str, city: str) -> dict | None:
    results = await _results(NOW_URL, {"key": api_key, "location": city, "language": "zh-Hans", "unit": "c"})
    if not results:
        return None
    now = results[0].get("now", {})
    temp = now.get("temperature", "0")
    return {
        "city": city,
        "desc": now.get("text", "未知"),
        "temp": temp,
        "feels_like": now.get("feels_like", temp),
        "humidity": now.get("humidity", "0"),
        "wind_speed": now.get("wind_speed", "0"),
    }


async def forecast(api_key: str, city: str) -> list[dict] | None:
    results = await _results(DAILY_URL, {
        "key": api_key, "location": city, "language": "zh-Hans", "unit": "c", "start": 0, "days": 3,
    })
    daily = results[0].get("daily", []) if results else []
    if not daily:
        return None
    keys = ("text_day", "text_night", "high", "low", "humidity", "wind_speed")
    defaults = {"text_day": "未知", "text_night": "未知"}
    return [
        {"date": d.get("date", "1970-01-01"), **{k: d.get(k, defaults.get(k, "0")) for k in keys}}
        for d in daily
    ]


async def suggestions(api_key: str, city: str) -> list[dict] | None:
    results = await _results(SUGGESTION_URL, {"key": api_key, "location": city, "language": "zh-Hans", "days": 1})
    suggestion = results[0].get("suggestion", {}) if results else {}
    if isinstance(suggestion, list):
        suggestion = suggestion[0] if suggestion else {}
    if not suggestion:
        return None
    return [
        {"name": name, "brief": (suggestion.get(key) or {}).get("brief", "无")}
        for key, name in SUGGESTION_NAMES.items()
    ]


def current_text(data: dict) -> str:
    return (
        f"当前天气：\n"
        f"城市: {data['city']}\n"
        f"天气: {data['desc']}\n"
        f"温度: {data['temp']}℃ (体感: {data['feels_like']}℃)\n"
        f"湿度: {data['humidity']}%\n"
        f"风速: {data['wind_speed']} km/h"
    )


def forecast_text(city: str, days: list[dict], tips: list[dict] | None) -> str:
    text = f"未来{len(days)}天天气预报\n城市: {city}\n"
    for day in days:
        text += (
            f"{day['date']}: 白天: {day['text_day']} - {day['high']}℃, "
            f"夜晚: {day['text_night']} - {day['low']}℃, "
            f"湿度: {day['humidity']}%, "
            f"风速: {day['wind_speed']} km/h\n"
        )
    if tips:
        text += "生活指数:\n" + "".join(f"{s['name']}: {s['brief']}\n" for s in tips)
    return text


Render = Callable[[str, dict], Awaitable[str]]


async def render_current(render: Render, data: dict) -> str:
    return await render(CURRENT_WEATHER_TEMPLATE, dict(data))


async def render_forecast(render: Render, city: str, days: list[dict], tips: list[dict] | None) -> str:
    return await render(FORECAST_TEMPLATE, {
        "city": city, "days": days, "total_days": len(days), "suggestions": tips or [],
    })


# ---------------------------------------------------------------- 原插件的 HTML 模板

CURRENT_WEATHER_TEMPLATE = """
<html>
<head>
  <meta charset="UTF-8"/>
  <style>
    html, body {
      margin: 0;
      padding: 0;
      width: 1280px; /* 确保匹配 render 预设的图片尺寸 */
      height: 720px;
      background-color: #fff;
    }
    .weather-container {
      width: 100%;
      height: 100%;
      padding: 8px;
      display: flex;
      flex-direction: column;
      justify-content: center; /* 垂直居中 */
      align-items: center; /* 水平居中 */
      background-color: #ffffff;
      color: #333;
      font-family: sans-serif;
      font-size: 30px;
      border: 1px solid #ddd;
      border-radius: 8px;
    }
    .weather-container h2 {
      margin-top: 0;
      color: #4e6ef2;
      text-align: center;
      font-size: 40px;
    }
    .weather-info {
      margin-bottom: 10px;
    }
    .source-info {
      border-top: 1px solid #ddd;
      margin-top: 12px;
      padding-top: 12px;
      font-size: 16px;
      color: #999;
    }
  </style>
</head>
<body>
  <div class="weather-container">
    <h2>当前天气</h2>
    
    <div class="weather-info">
      <strong>城市:</strong> {{ city }}
    </div>
    <div class="weather-info">
      <strong>天气:</strong> {{ desc }}
    </div>
    <div class="weather-info">
      <strong>温度:</strong> {{ temp }}℃ (体感: {{ feels_like }}℃)
    </div>
    <div class="weather-info">
      <strong>湿度:</strong> {{ humidity }}%
    </div>
    <div class="weather-info">
      <strong>风速:</strong> {{ wind_speed }} km/h
    </div>
    
    <div class="source-info">
      数据来源: 心知天气（Seniverse） 免费API
    </div>
  </div>
</body>
</html>
"""

FORECAST_TEMPLATE = """
<html>
<head>
  <meta charset="UTF-8"/>
  <style>
    html, body {
      margin: 0;
      padding: 0;
      width: 1280px;
      height: 720px;
      background-color: #fff;
    }
    .forecast-container {
      width: 100%;
      height: 100%;
      padding: 8px;
      display: flex;
      flex-direction: column;
      justify-content: center;
      align-items: center;
      background-color: #fff;
      color: #333;
      font-family: sans-serif;
      font-size: 30px;
      border: 1px solid #ddd;
      border-radius: 8px;
    }
    .forecast-container h2 {
      margin-top: 0;
      color: #4e6ef2;
      text-align: center;
      font-size: 40px;
    }
    .city-info {
      margin-bottom: 8px;
    }
    .day-item {
      margin-bottom: 8px;
      border-bottom: 1px solid #eee;
      padding-bottom: 4px;
    }
    .day-title {
      font-weight: bold;
      color: #4e6ef2;
      margin-bottom: 4px;
    }
    .source-info {
      font-size: 16px;
      color: #999;
      margin-top: 12px;
      border-top: 1px solid #ddd;
      padding-top: 8px;
    }
  </style>
</head>
<body>
  <div class="forecast-container">
    <h2>未来{{ total_days }}天天气预报</h2>
    <div class="city-info">
      <strong>城市:</strong> {{ city }}
    </div>

    {% for day in days %}
    <div class="day-item">
      <div class="day-title">{{ day.date }}</div>
      <div><strong>白天:</strong> {{ day.text_day }} — {{ day.high }}℃</div>
      <div><strong>夜晚:</strong> {{ day.text_night }} — {{ day.low }}℃</div>
      <div><strong>湿度:</strong> {{ day.humidity }}%  <strong>风速:</strong> {{ day.wind_speed }} km/h</div>
    </div>
    {% endfor %}

    <div class="source-info">
      数据来源: 心知天气（Seniverse） 免费API
    </div>
  </div>
</body>
</html>
"""
