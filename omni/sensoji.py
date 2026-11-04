"""浅草寺抽签（原 astrbot_plugin_sensoji_pro），签文图片排版与原插件一致。"""

from __future__ import annotations

import json
import random
import re
import time
import uuid
from datetime import date
from pathlib import Path

from PIL import ImageDraw, ImageFont
from PIL.Image import new as ImageNew

from .sensoji_data import sensoji_results

EXPLAIN_PROMPT = (
    "回复要求：\n"
    "1. 如果用户尚未抽签，告知用户`需要先抽签，再进行解签`。\n"
    "2. 如果用户已抽签，则分析签文内容并提供详细解释，包括抽签结果的意义、可能的象征以及建议。\n"
    "3. 基于解签内容提炼出重点建议，提供一些具体与实际问题相关的指导意见。\n"
    "4. 保持语气友好、亲切，确保签文解析准确且易于理解，尽量使用简短的一段话结束。\n"
    "5. 基于角色以合适的语气、称呼等，生成符合人设的回答。\n"
    "6. 使用纯文本，不要分点，禁用任何Markdown或代码块。\n\n"
    "内容: {message}"
)


def fortune_message(item: dict) -> str:
    return (
        f"{item['result']}\n\n"
        f"诗文：{item['poetry']}\n\n"
        f"解析：{item['interpretation']}\n\n"
        f"建议：{item['suggestion']}\n\n"
        f"运势细节：{item['horoscope_details']}"
    )


def remove_escaped_emojis(text: str) -> str:
    return re.sub(r"&&\w+&&", "", text or "")


class Sensoji:
    def __init__(self, data_dir: Path, font_dir: Path):
        self.data_dir = Path(data_dir)
        self.font_dir = Path(font_dir)
        self.output_dir = self.data_dir / "sensoji_output"
        self.results_file = self.data_dir / "sensoji_daily_results.json"
        self.counts_file = self.data_dir / "sensoji_change_counts.json"

    # ---- 存储
    @staticmethod
    def _load(path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _save(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 签文
    def today_result(self, user_id: str) -> str | None:
        record = self._load(self.results_file).get(str(user_id))
        return record["result"] if record and record.get("date") == str(date.today()) else None

    def draw(self, user_id: str, force_new: bool = False) -> str:
        """当天已抽过则返回原结果；force_new 时重新抽（转运）。"""
        user_id, today = str(user_id), str(date.today())
        results = self._load(self.results_file)
        record = results.get(user_id)
        if record and record.get("date") != today:
            results.pop(user_id)
            record = None
        if record is None or force_new:
            record = {"date": today, "result": fortune_message(random.choice(sensoji_results))}
            results[user_id] = record
            self._save(self.results_file, results)
        return record["result"]

    def change(self, user_id: str, max_times: int) -> tuple[str, str | None, int]:
        """转运。返回 (状态, 签文, 今日已转运次数)；状态为 ok / limit。"""
        user_id, today = str(user_id), str(date.today())
        counts = self._load(self.counts_file)
        entry = counts.get(user_id)
        if not entry or entry.get("date") != today:
            entry = {"date": today, "count": 0}
        if max_times > 0 and entry["count"] >= max_times:
            return "limit", None, entry["count"]
        had_today = self.today_result(user_id) is not None
        result = self.draw(user_id, force_new=had_today)
        if had_today:
            entry["count"] += 1
            counts[user_id] = entry
            self._save(self.counts_file, counts)
        return "ok", result, entry["count"]

    # ---- 图片
    def cleanup(self, keep_days: int) -> None:
        if keep_days <= 0 or not self.output_dir.exists():
            return
        deadline = time.time() - keep_days * 86400
        for file in self.output_dir.glob("*.png"):
            try:
                if file.stat().st_mtime < deadline:
                    file.unlink()
            except OSError:
                pass

    def render(self, title: str, message: str, keep_days: int = 1) -> str:
        self.cleanup(keep_days)
        return render_fortune({"title": title, "message": message}, self.font_dir, self.output_dir)


# ---------------------------------------------------------------- 原插件的签文绘图


def render_fortune(data: dict, font_dir: Path, output_dir: Path) -> str:
    """优化版签文生成（智能分段+混合布局）"""
    # 配置参数
    img_width = 800
    padding = 60
    colors = {
        "background": "#FFF5E6",
        "title": (188, 42, 26),
        "text": (51, 51, 51),
        "border": (205, 128, 83),
        "stamp": (227, 66, 52)
    }

    # 加载字体
    title_font = ImageFont.truetype(str(font_dir / "FZSTK.ttf"), 84)
    sx_content_font = ImageFont.truetype(str(font_dir / "SSQFT.ttf"), 66)
    s_content_font = ImageFont.truetype(str(font_dir / "SYST.otf"), 42)
    h_content_font = ImageFont.truetype(str(font_dir / "BGTXT.ttf"), 38)
    stamp_font = ImageFont.truetype(str(font_dir / "STLITI.ttf"), 46)

    def smart_segment(text: str) -> list:
        """智能分段逻辑优化"""
        markers = ["解析：", "建议：", "运势细节："]
        segments = []
        buffer = ""
        for line in text.split('\n'):
            line = line.strip()
            if any(line.startswith(m) for m in markers):
                if buffer:
                    segments.append(buffer)
                    buffer = ""
                segments.append("◆" + line)
            else:
                buffer += line + " "
        if buffer:
            segments.append(buffer)
        return segments

    def calculate_layout(segments: list) -> tuple:
        """混合布局计算（竖排+横排）"""
        layout_data = []
        total_height = padding + 120
        right_margin = 80
        left_margin = 80
        first_vertical = True

        for seg in segments:
            if not seg.strip():
                continue

            seg = seg.replace('\u200b', '').replace('\ufeff', '')

            is_horizontal = any(seg.startswith(f"◆{m}") for m in ["解析：", "建议：", "运势细节："])

            if is_horizontal:
                # seg = seg.lstrip("◆")
                available_width = img_width - right_margin - left_margin

                lines = []
                current_line = []
                current_width = 0
                for char in seg:
                    bbox = h_content_font.getbbox(char)
                    char_width = bbox[2] - bbox[0]
                    if current_width + char_width > available_width:
                        lines.append("".join(current_line))
                        current_line = [char]
                        current_width = char_width
                    else:
                        current_line.append(char)
                        current_width += char_width
                if current_line:
                    lines.append("".join(current_line))

                line_height = 45
                seg_height = len(lines) * line_height + 50

                layout_data.append({
                    "type": "horizontal",
                    "content": lines,
                    "height": seg_height,
                    "line_height": line_height
                })
                total_height += seg_height
            else:
                if first_vertical and "　" in seg and " 诗文：" in seg and "；" in seg:
                    first_vertical = False
                    parts = seg.split(" 诗文：", 1)
                    fortune_info = parts[0]
                    poem_parts = parts[1].split("；", 1)
                    if len(poem_parts) == 2:
                        line1 = poem_parts[0] + '；'
                        line2 = poem_parts[1]

                        col1 = [char for char in fortune_info]
                        col2 = [char for char in "诗文："]
                        col3 = [char for char in line1]
                        col4 = [char for char in line2]

                        max_len = max(len(col1), len(col2), len(col3), len(col4))
                        seg_height = max_len * 45 + 40  # Using line_height and section_spacing

                        layout_data.append({
                            "type": "vertical",
                            "columns": [col1, col3, col4],
                            "height": seg_height,
                            "col_width": 60,  # Adjust as needed
                            "line_height": 45
                        })
                        total_height += seg_height
                        continue  # Skip the old vertical logic
                else:
                    # 默认采用横排格式
                    available_width = img_width - right_margin - left_margin

                    lines = []
                    current_line = []
                    current_width = 0
                    for char in seg:
                        bbox = h_content_font.getbbox(char)
                        char_width = bbox[2] - bbox[0]
                        if current_width + char_width > available_width:
                            lines.append("".join(current_line))
                            current_line = [char]
                            current_width = char_width
                        else:
                            current_line.append(char)
                            current_width += char_width
                    if current_line:
                        lines.append("".join(current_line))

                    line_height = 45
                    seg_height = len(lines) * line_height + 50

                    layout_data.append({
                        "type": "horizontal",
                        "content": lines,
                        "height": seg_height,
                        "line_height": line_height
                    })
                    total_height += seg_height

        return layout_data, total_height + 300, right_margin

    # 创建画布
    segments = smart_segment(data.get("message"))
    layout_data, total_height, right_margin = calculate_layout(segments)

    img = ImageNew("RGB", (img_width, total_height), colors["background"])
    draw = ImageDraw.Draw(img)

    # 绘制背景纹理
    for i in range(0, img_width, 6):
        draw.line([(i, 0), (i, total_height)], fill=(230, 220, 210), width=1)

    # 绘制边框
    border_width = 10
    draw.rectangle(
        [border_width, border_width, img_width - border_width, total_height - border_width],
        outline=colors["border"],
        width=2
    )

    # 绘制标题
    title = data.get("title")
    title_bbox = draw.textbbox((0, 0), title, font=title_font)
    title_x = (img_width - title_bbox[2]) // 2
    draw.text((title_x, padding + 20), title, fill=colors["title"], font=title_font)

    # 混合排版逻辑
    y_pos = padding + title_bbox[3] + 140
    left_margin = 80

    for seg_data in layout_data:
        if seg_data["type"] == "horizontal":
            y_pos += 30
            for line in seg_data["content"]:
                draw.text((left_margin, y_pos), line, fill=colors["text"], font=h_content_font)
                y_pos += seg_data["line_height"]
            y_pos += 50
        elif seg_data["type"] == "vertical":
            columns = seg_data["columns"]
            col_width = seg_data["col_width"]
            line_height = seg_data["line_height"]
            spacing = 150  # 增加竖排的列间距
            start_x = left_margin + 80
            max_y = y_pos

            for col_idx, column in enumerate(columns):
                current_y = y_pos
                if col_idx == 0:
                    for char in column:
                        draw.text(
                            (start_x + col_idx * col_width + col_idx * spacing, current_y),
                            char,
                            fill=colors["text"],
                            font=sx_content_font
                        )
                        current_y += line_height * 1.6
                else:
                    for char in column:
                        draw.text(
                            (start_x + col_idx * col_width + col_idx * spacing, current_y),
                            char,
                            fill=colors["text"],
                            font=s_content_font
                        )
                        current_y += line_height
                max_y = max(max_y, current_y)
            y_pos = max_y

    # 印章绘制优化 - 调整位置以重合
    stamp_text = "浅草寺"
    stamp_bbox = draw.textbbox((0, 0), stamp_text, font=stamp_font)
    stamp_size = max(stamp_bbox[2] - stamp_bbox[0], 50)

    # 计算印章可以出现的右下角区域
    right_margin_buffer = 20  # 留出一些右边距
    bottom_margin_buffer = 20  # 留出一些底部边距

    min_stamp_x = int(img_width * 0.6)
    max_stamp_x = img_width - stamp_size - right_margin_buffer

    min_stamp_y = int(total_height * 0.8)
    max_stamp_y = total_height - stamp_size - bottom_margin_buffer

    # 确保 min_stamp_x 不大于 max_stamp_x
    if min_stamp_x > max_stamp_x:
        min_stamp_x = max_stamp_x  # 或者你可以根据需要设置一个默认值

    # 确保 min_stamp_y 不大于 max_stamp_y
    if min_stamp_y > max_stamp_y:
        min_stamp_y = max_stamp_y  # 或者你可以根据需要设置一个默认值

    # 生成随机的 x 和 y 坐标
    stamp_x = random.randint(min_stamp_x, max_stamp_x)
    stamp_y = random.randint(min_stamp_y, max_stamp_y)

    draw.ellipse(
        [stamp_x, stamp_y, stamp_x + stamp_size, stamp_y + stamp_size],
        outline=colors["stamp"],
        width=2
    )
    # 调整文字位置，使其居中于随机生成的圆内
    text_x = stamp_x + (stamp_size - stamp_bbox[2]) // 2
    text_y = stamp_y + (stamp_size - stamp_bbox[3]) // 2 - (stamp_bbox[1])  # 稍微调整文字的垂直位置
    draw.text(
        (text_x, text_y),
        stamp_text,
        fill=colors["stamp"],
        font=stamp_font
    )

    # 最终裁剪
    img = img.crop((0, 0, img_width, total_height))

    # 保存文件
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"sign_{int(time.time())}_{uuid.uuid4().hex[:6]}.png"
    img.save(output_path, quality=95, optimize=True)

    return str(output_path.resolve())

