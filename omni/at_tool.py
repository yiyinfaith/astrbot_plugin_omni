"""@ 工具：引导 LLM 在 QQ 群聊里用 [at:QQ号] 标签 @ 群友，发送前转成真实的 At。

QQ 私聊发 At 组件会让整条消息发送失败，所以只有 QQ（aiocqhttp）群聊才注入提示词、
提供成员查询工具；其他场景里模型仍然写出的标签，发送前直接删掉。
"""

from __future__ import annotations

import json
import re
from typing import Any

from astrbot.api import logger
from astrbot.core.agent.message import TextPart
from astrbot.core.message.components import At, Plain

from .markdown2img import MD_PATTERN

TOOL_NAME = "get_group_members"
# 一次最多返回的成员数，大群全量返回会撑爆上下文
MAX_MEMBERS = 200
# 群主、管理员排在前面，被截断时也能查到
ROLE_ORDER = {"owner": 0, "admin": 1}
ROLE_NAMES = {"owner": "群主", "admin": "管理员", "member": "成员"}

TAG_PATTERN = re.compile(r"\[at:(\d+|all)\]")
TAG_AND_SPACES = re.compile(r"\[at:(?:\d+|all)\][ \t]*")
# At 后面补的零宽字符，避免后面的文字和 @ 粘连
AFTER_AT = "​ ​"

PERMISSION_CACHE_KEY = "_omni_at_all_permission"
PERMISSION_NOTE_TAG = "<at_all_permission>"
PERMISSION_RULE = (
    "\n【@全体权限规则】\n"
    "只有群主、群管理员或 Bot 超级管理员才能艾特全体成员。"
    "\n本轮操作者是否具备该权限，以本轮用户消息后附带的权限说明为准。"
)
ALLOWED_NOTE = (
    "\n<at_all_permission>"
    "\n当前操作者具备@全体权限。"
    "\n如用户明确要求且场景确有必要，你可以输出 [at:all]。"
    "\n</at_all_permission>"
)
DENIED_NOTE = (
    "\n<at_all_permission>"
    "\n当前操作者不具备@全体权限。"
    "\n原因：{reason}"
    "\n禁止输出 [at:all]。"
    "\n如果用户要求@全体，请直接用自然语言说明无法执行，不要输出任何 @全体 标签。"
    "\n</at_all_permission>"
)


def supports_at(event: Any) -> bool:
    """当前会话能否真正 @ 人：只有 QQ（aiocqhttp）群聊可以。"""
    return event.get_platform_name() == "aiocqhttp" and bool(event.get_group_id())


def normalize_prompt(text: object) -> str:
    """配置编辑器可能把换行存成字面的 \\n，这里还原。"""
    if not isinstance(text, str):
        return ""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if "\n" not in normalized and "\\n" in normalized:
        normalized = normalized.replace("\\r\\n", "\n").replace("\\n", "\n")
    return normalized


def has_tag(chain: list) -> bool:
    return any(isinstance(comp, Plain) and "[at:" in (comp.text or "") for comp in chain)


def strip_tags(chain: list) -> list:
    """删掉文字里的标签，只剩空白的文字段一并去掉。"""
    result = []
    for comp in chain:
        if isinstance(comp, Plain) and "[at:" in (comp.text or ""):
            text = TAG_AND_SPACES.sub("", comp.text)
            if not text.strip():
                continue
            comp = Plain(text)
        result.append(comp)
    return result


def _split_tags(text: str, allow_all: bool) -> list:
    """把一段文字拆成 Plain 和 At。没有权限的 @全体 降级成普通文字。"""
    parts: list = []
    last = 0
    for match in TAG_PATTERN.finditer(text):
        start, end = match.span()
        if start > last:
            parts.append(Plain(text[last:start]))
        target = match.group(1)
        if target == "all" and not allow_all:
            parts.append(Plain("@全体成员"))
        else:
            parts.extend([At(qq=target), Plain(" ")])
        last = end
    if last < len(text):
        parts.append(Plain(text[last:]))
    return parts


def _convert_text(text: str, allow_all: bool) -> list:
    """<md> 块要保持完整，留给 Markdown 转图片；图片里 @ 不了人，块内的标签直接删掉。"""
    parts: list = []
    for piece in MD_PATTERN.split(text):
        if piece.startswith("<md>") and piece.endswith("</md>"):
            parts.append(Plain(TAG_AND_SPACES.sub("", piece)))
        elif piece:
            parts.extend(_split_tags(piece, allow_all))
    return parts


def _trim_spaces_around(chain: list, index: int) -> None:
    """去掉 At 前后紧邻文字里的空格。"""
    for prev in reversed(chain[:index]):
        if isinstance(prev, Plain):
            prev.text = prev.text.rstrip(" \t")
            break
        if not isinstance(prev, At):
            break
    for following in chain[index + 1:]:
        if isinstance(following, Plain):
            following.text = following.text.lstrip(" \t")
            if following.text:
                break
        elif not isinstance(following, At):
            break


def convert_tags(chain: list, allow_all: bool) -> list:
    """把消息链里的标签转成 At，并整理 At 前后的空白。"""
    result: list = []
    for comp in chain:
        if isinstance(comp, Plain):
            result.extend(_convert_text(comp.text or "", allow_all))
        else:
            result.append(comp)

    for index, comp in enumerate(result):
        if isinstance(comp, At):
            _trim_spaces_around(result, index)

    index = 0
    while index < len(result):
        if isinstance(result[index], At):
            following = next((c for c in result[index + 1 :] if isinstance(c, Plain)), None)
            if following is None:
                result.insert(index + 1, Plain(AFTER_AT))
            else:
                following.text = AFTER_AT + following.text
        index += 1
    return [comp for comp in result if not (isinstance(comp, Plain) and not comp.text)]


async def _query_permission(event: Any) -> tuple[bool, str]:
    if event.is_admin():
        return True, ""
    sender_id = event.get_sender_id()
    if not sender_id:
        return False, "无法识别发送者身份"
    group_id = event.get_group_id()
    try:
        info = await event.bot.api.call_action(
            "get_group_member_info", group_id=group_id, user_id=sender_id, no_cache=True
        )
        role = str((info or {}).get("role", "member")).lower()
    except Exception as exc:
        logger.warning(f"[@工具] 查询@全体权限失败: group_id={group_id}, user_id={sender_id}, error={exc}")
        return False, "查询群成员权限失败"
    if role in {"owner", "admin"}:
        return True, ""
    return False, f"{sender_id} 不是群主、管理员或 Bot 超级管理员"


async def _at_all_permission(event: Any) -> tuple[bool, str]:
    """同一条消息只查一次：请求前和发送前都要用到。"""
    cached = event.get_extra(PERMISSION_CACHE_KEY)
    if cached is not None:
        return cached
    result = await _query_permission(event)
    event.set_extra(PERMISSION_CACHE_KEY, result)
    return result


def _append_note(req: Any, note: str) -> None:
    """权限结论随发言人变化，放到本轮用户消息后面，不写进系统提示词，也不进历史。"""
    if any(PERMISSION_NOTE_TAG in (getattr(part, "text", "") or "") for part in req.extra_user_content_parts):
        return
    req.extra_user_content_parts.append(TextPart(text=note).mark_as_temp())


class AtTool:
    def __init__(self, config: dict):
        self.verify_permission = bool(config.get("permission_verification", True))
        self.prompt = normalize_prompt(config.get("llm_prompt", "")).strip()

    async def inject(self, event: Any, req: Any) -> None:
        """只在 QQ 群聊注入 @ 的使用说明。"""
        if not supports_at(event):
            return
        if self.prompt:
            req.system_prompt = f"{req.system_prompt or ''}\n\n{self.prompt}"
        if not self.verify_permission:
            return
        req.system_prompt = f"{req.system_prompt or ''}{PERMISSION_RULE}"
        allowed, reason = await _at_all_permission(event)
        _append_note(req, ALLOWED_NOTE if allowed else DENIED_NOTE.format(reason=reason))

    async def process_result(self, event: Any) -> None:
        """发送前处理标签：QQ 群聊转成 At，其他场景删掉，避免私聊整条消息发送失败。"""
        result = event.get_result()
        chain = getattr(result, "chain", None)
        if not chain or not has_tag(chain):
            return
        if not supports_at(event):
            result.chain = strip_tags(chain)
            return
        allow_all = True
        if self.verify_permission and any(
            isinstance(comp, Plain) and "[at:all]" in (comp.text or "") for comp in chain
        ):
            allow_all, reason = await _at_all_permission(event)
            if not allow_all:
                logger.info(
                    f"[@工具] 拦截越权@全体: group_id={event.get_group_id()}, "
                    f"sender_id={event.get_sender_id()}, reason={reason}"
                )
        result.chain = convert_tags(chain, allow_all)


async def get_group_members(event: Any, keyword: Any = "") -> str:
    """查询当前群成员，返回给 LLM 的 JSON。"""
    if not supports_at(event):
        return _error("只有 QQ 群聊可以查询群成员。")
    try:
        raw_members = await event.bot.api.call_action("get_group_member_list", group_id=event.get_group_id())
    except Exception as exc:
        logger.error(f"[@工具] 查询群成员失败: {exc}")
        return _error("查询群成员失败，请稍后再试。")
    if not raw_members:
        return _error("无法获取成员列表或机器人权限不足。")

    needle = str(keyword or "").strip().casefold()
    members = []
    for member in raw_members:
        user_id = str(member.get("user_id", ""))
        nickname = str(member.get("nickname", "") or "")
        card = str(member.get("card", "") or "")
        if needle and not any(needle in value.casefold() for value in (user_id, nickname, card)):
            continue
        members.append((member.get("role"), user_id, nickname, card))
    members.sort(key=lambda item: ROLE_ORDER.get(item[0], 2))

    output: dict[str, Any] = {
        "status": "success",
        "group_id": event.get_group_id(),
        "count": len(members),
        "members": [
            {"user_id": user_id, "nickname": nickname, "group_card": card or "无", "role": ROLE_NAMES.get(role, "成员")}
            for role, user_id, nickname, card in members[:MAX_MEMBERS]
        ],
    }
    if len(members) > MAX_MEMBERS:
        output["note"] = f"匹配的成员太多，只返回前 {MAX_MEMBERS} 个，请用 keyword 缩小范围。"
    return json.dumps(output, ensure_ascii=False, separators=(",", ":"))


def _error(message: str) -> str:
    return json.dumps({"status": "error", "message": message}, ensure_ascii=False)
