"""QQ 点赞（原 astrbot_plugin_zanwo），仅 aiocqhttp。"""

from __future__ import annotations

import random

from astrbot.api import logger

SUCCESS_RESPONSES = [
    "👍{total_likes}",
    "赞了赞了",
    "点赞成功！",
    "给{username}点了{total_likes}个赞",
    "赞送出去啦！一共{total_likes}个哦！",
    "为{username}点赞成功！总共{total_likes}个！",
    "点了{total_likes}个，快查收吧！",
    "赞已送达，请注意查收~ 一共{total_likes}个！",
    "给{username}点了{total_likes}个赞，记得回赞哟！",
    "赞了{total_likes}次，看看收到没？",
    "点了{total_likes}赞，没收到可能是我被风控了",
]
LIMIT_RESPONSES = [
    "今天给{username}的赞已达上限",
    "赞了那么多还不够吗？",
    "{username}别太贪心哟~",
    "今天赞过啦！",
    "今天已经赞过啦~",
    "已经赞过啦~",
    "还想要赞？不给了！",
    "已经赞过啦，别再点啦！",
]
STRANGER_RESPONSES = [
    "不加好友不赞",
    "我和你有那么熟吗？",
    "你谁呀？",
    "你是我什么人凭啥要我赞你？",
    "不想赞你这个陌生人",
    "我不认识你，不赞！",
    "加我好友了吗就想要我赞你？",
    "滚！",
]


async def like_users(client, ids: list[str]) -> str:
    """给每个用户最多点 50 个赞（每次 10 个，共 5 次），返回回复文本。"""
    replies = []
    for uid in ids:
        total, error_reply, username = 0, "", "未知用户"
        try:
            info = await client.get_stranger_info(user_id=int(uid))
            username = (info or {}).get("nickname", username)
        except Exception:
            pass
        for _ in range(5):
            try:
                await client.send_like(user_id=int(uid), times=10)
                total += 10
            except Exception as exc:
                message = str(exc)
                if "已达" in message:
                    error_reply = random.choice(LIMIT_RESPONSES)
                elif "权限" in message:
                    error_reply = "你设了权限不许陌生人赞你"
                elif "ActionFailed" in type(exc).__name__ or "retcode" in message:
                    error_reply = random.choice(STRANGER_RESPONSES)
                else:
                    logger.warning(f"[点赞] 给 {uid} 点赞失败: {exc}")
                    error_reply = error_reply or "点赞失败了，稍后再试试吧"
                break
        reply = random.choice(SUCCESS_RESPONSES) if total > 0 else (error_reply or random.choice(STRANGER_RESPONSES))
        replies.append(reply.replace("{username}", str(username)).replace("{total_likes}", str(total)))
    return "\n".join(replies).strip()

