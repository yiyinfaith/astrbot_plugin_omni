"""对话回退（原 astrbot_plugin_llm_amnesia）：删掉最近几轮对话，可在下一次对话前撤销。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field

MAX_ROUNDS = 10
EXPIRE_SECONDS = 30 * 60
USAGE = "用法：/回退 [轮数]（1-10，默认 1）；/取消回退 恢复刚才删掉的对话"
_SYSTEM_MARKERS = ("<RAG", "Memory>", "history_knowledge")
_REMINDER = re.compile(r"<system_reminder>.*?(</system_reminder>|$)", re.S)


@dataclass
class Backup:
    conversation_id: str
    messages: list[dict]
    rounds: int
    created: float = field(default_factory=time.time)


def parse_rounds(text: str) -> int | None:
    """解析回退轮数，空白默认 1，非法返回 None。"""
    text = (text or "").strip()
    if not text:
        return 1
    try:
        rounds = int(text)
    except ValueError:
        return None
    return rounds if 1 <= rounds <= MAX_ROUNDS else None


def load_history(raw) -> list[dict]:
    """解析会话历史，兼容被双重序列化的旧数据。"""
    if not raw:
        return []
    data = raw
    for _ in range(2):
        if isinstance(data, str):
            data = json.loads(data)
    if not isinstance(data, list):
        raise ValueError("对话历史不是消息列表")
    return data


def split_rounds(history: list[dict], rounds: int) -> tuple[list[dict], list[dict]] | None:
    """以每条 user 消息为一轮的起点切出最近 rounds 轮；被中断、没有回复的提问也算一轮。"""
    starts = [i for i, msg in enumerate(history) if isinstance(msg, dict) and msg.get("role") == "user"]
    if len(starts) < rounds:
        return None
    cut = starts[-rounds]
    return history[:cut], history[cut:]


def _plain(content) -> str:
    """提取消息里用户能看到的文字，去掉 AstrBot 注入的 system_reminder、知识库等内容。"""
    if isinstance(content, list):
        parts = [
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        text = "".join(p for p in parts if not any(m in p for m in _SYSTEM_MARKERS))
    else:
        text = content if isinstance(content, str) else str(content or "")
    return _REMINDER.sub("", text).strip()


def _short(text: str, limit: int = 50) -> str:
    text = text.replace("\n", " ")
    return text[:limit] + ("..." if len(text) > limit else "")


def preview(messages: list[dict]) -> str:
    rounds: list[list[str]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "user":
            rounds.append([_plain(msg.get("content")), ""])
        elif role == "assistant" and rounds:
            text = _plain(msg.get("content"))
            if not text and msg.get("tool_calls"):
                text = "[工具调用]"
            if text:
                rounds[-1][1] = text
    lines = []
    for i, (user, bot) in enumerate(rounds, 1):
        lines.append(f"第 {i} 轮:\n👤: {_short(user) or '[空内容]'}\n🤖: {_short(bot) or '[未完成/已停止]'}")
    return "\n\n".join(lines)


class Rewinder:
    """按会话保存被删掉的消息。会话里发起新的对话后备份作废，30 分钟后也会过期。"""

    def __init__(self) -> None:
        self.backups: dict[str, Backup] = {}
        self.lock = asyncio.Lock()

    def _prune(self) -> None:
        now = time.time()
        for key in [k for k, b in self.backups.items() if now - b.created > EXPIRE_SECONDS]:
            del self.backups[key]

    def drop(self, umo: str) -> None:
        self.backups.pop(umo, None)

    async def rewind(self, conv_mgr, umo: str, rounds: int) -> str:
        async with self.lock:
            self._prune()
            cid = await conv_mgr.get_curr_conversation_id(umo)
            conversation = await conv_mgr.get_conversation(umo, cid) if cid else None
            if not conversation:
                return "当前没有对话 ❌"
            try:
                history = load_history(conversation.history)
            except (ValueError, TypeError):
                return "对话历史格式异常，无法回退 ❌"
            parts = split_rounds(history, rounds)
            if parts is None:
                count = sum(1 for m in history if isinstance(m, dict) and m.get("role") == "user")
                return f"只有 {count} 轮对话，无法回退 {rounds} 轮 ❌"
            kept, removed = parts
            await conv_mgr.update_conversation(umo, conversation.cid, history=kept)
            old = self.backups.get(umo)
            if old and old.conversation_id == conversation.cid:
                self.backups[umo] = Backup(conversation.cid, removed + old.messages, rounds + old.rounds)
            else:
                self.backups[umo] = Backup(conversation.cid, removed, rounds)
        return f"🗑️ 已回退 {rounds} 轮对话:\n\n{preview(removed)}\n\n💡 下一次对话前发送 /取消回退 可以恢复"

    async def restore(self, conv_mgr, umo: str) -> str:
        async with self.lock:
            self._prune()
            backup = self.backups.get(umo)
            if backup is None:
                return "没有可恢复的对话 ❌"
            conversation = await conv_mgr.get_conversation(umo, backup.conversation_id)
            if conversation is None:
                self.drop(umo)
                return "原对话已不存在，无法恢复 ❌"
            try:
                history = load_history(conversation.history)
            except (ValueError, TypeError):
                return "对话历史格式异常，无法恢复 ❌"
            await conv_mgr.update_conversation(umo, backup.conversation_id, history=history + backup.messages)
            self.drop(umo)
        return f"✅ 已恢复 {backup.rounds} 轮对话"
