"""二维码生成与识别（原 astrbot_plugin_qrcode，补充了二维码转链接）。"""

from __future__ import annotations

import io
import re

URL_PATTERN = re.compile(r"https?://\S+")
WAIT_TEXT = "📷 请在 {seconds} 秒内发送二维码图片，我来识别成链接\n生成二维码请发送：/二维码 链接或文字"


def make_qr(text: str) -> bytes:
    import qrcode

    match = URL_PATTERN.search(text)
    content = match.group() if match else text.strip()
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=4)
    qr.add_data(content)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
    return buf.getvalue()


def _decode_pyzbar(image) -> list[str]:
    try:
        from pyzbar.pyzbar import decode
    except Exception:  # 没装 zbar 动态库时 import 会失败
        return []
    results = []
    for obj in decode(image):
        text = obj.data.decode("utf-8", errors="replace").strip()
        if text and text not in results:
            results.append(text)
    return results


def _decode_cv2(image) -> list[str]:
    try:
        import cv2
        import numpy as np
    except Exception:
        return []
    arr = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2BGR)
    detector = cv2.QRCodeDetector()
    try:
        ok, texts, _, _ = detector.detectAndDecodeMulti(arr)
        found = [t for t in (texts if ok else []) if t]
    except Exception:
        found = []
    if not found:
        text, _, _ = detector.detectAndDecode(arr)
        found = [text] if text else []
    return list(dict.fromkeys(found))


def decode_qr(data: bytes) -> list[str]:
    from PIL import Image

    image = Image.open(io.BytesIO(data))
    image.load()
    results = _decode_pyzbar(image)
    if not results:
        results = _decode_cv2(image)
    if not results and max(image.size) < 600:  # 小图放大再试一次
        scale = 600 / max(image.size)
        big = image.resize((int(image.width * scale), int(image.height * scale)))
        results = _decode_pyzbar(big) or _decode_cv2(big)
    return results


def format_results(results: list[str]) -> str:
    if not results:
        return "❌ 未检测到二维码或解析失败"
    if len(results) == 1:
        label = "🔗 链接" if URL_PATTERN.fullmatch(results[0]) else "✅ 解析结果"
        return f"{label}：{results[0]}"
    lines = [f"✅ 识别到 {len(results)} 个二维码："]
    lines += [f"{i}. {text}" for i, text in enumerate(results, 1)]
    return "\n".join(lines)
