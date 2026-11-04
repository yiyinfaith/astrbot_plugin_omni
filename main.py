"""综合插件：把零散的娱乐与工具小功能集中到一个插件，每个功能独立开关。

AstrBot 按处理函数所在模块绑定插件，所以带装饰器的函数都留在本文件，具体实现放在 omni/ 下。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.provider.entities import ProviderRequest

from .omni import (
    aicu,
    fun,
    ipquery,
    markdown2img,
    plot_tools,
    qrcode_tool,
    rewind,
    runes,
    simple,
    tagger,
    translation,
    weather,
    zanwo,
)
from .omni.browser import SharedBrowser
from .omni.common import (
    PLUGIN_DIR,
    PLUGIN_NAME,
    ImageWaiter,
    TempFiles,
    command_rest,
    fetch,
    image_bytes,
    image_suffix,
    message_images,
    section,
    sender_key,
)
from .omni.gengtu import Gengtu
from .omni.safebooru import Safebooru
from .omni.sensoji import EXPLAIN_PROMPT, Sensoji, remove_escaped_emojis

# 关闭某个功能时，从 LLM 请求里移除它的函数工具
FEATURE_TOOLS = {
    "calculate": ("calculate",),
    "math_plotter": ("plot_2d", "plot_3d", "plot_vectors"),
    "sensoji": ("explain_fortune",),
    "weather": ("get_weather",),
}
# 处理函数 -> 所属功能。功能关闭时把这些指令设为禁用，不再被匹配，也不会出现在指令列表里
HANDLER_FEATURES = {
    "left_brain": "left_brain", "history_today": "history_today", "dot_matrix": "dot_matrix",
    "look_leg": "look_leg", "emoji_mix": "emoji_mix", "jrrp": "jrrp", "sensoji_draw": "sensoji", "sensoji_change": "sensoji",
    "sensoji_explain": "sensoji", "runes_divination": "runes", "gengtu_question": "gengtu",
    "gengtu_answer": "gengtu", "random_image": "safebooru", "tag": "tagger", "qrcode": "qrcode",
    "weather_cmd": "weather", "translate_cmd": "translation", "ip_query": "ipquery", "whois_query": "ipquery",
    "aicu_uid": "aicu", "like_me": "zanwo", "subscribe_like": "zanwo", "unsubscribe_like": "zanwo",
    "rewind_cmd": "rewind", "cancel_rewind": "rewind",
}
TAG_COMMANDS = {"tag"}
QR_COMMANDS = {"二维码", "扫码"}


class OmniPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.temp = TempFiles(self.data_dir / "tmp", max_age=3600)
        self.browser = SharedBrowser()
        self.gengtu = Gengtu()
        self.safebooru = Safebooru(self.data_dir)
        self.sensoji = Sensoji(self.data_dir, PLUGIN_DIR / "fonts")
        self.tag_waiter = ImageWaiter()
        self.qr_waiter = ImageWaiter()
        self.rewinder = rewind.Rewinder()
        self._math = None
        self._background: set[asyncio.Task] = set()

    async def initialize(self):
        self._apply_feature_switches()

    def _apply_feature_switches(self) -> None:
        """只改内存里的指令启用状态，不写数据库；修改配置后插件重载时重新生效。"""
        from astrbot.core.star.star_handler import star_handlers_registry

        for handler in star_handlers_registry.get_handlers_by_module_name(type(self).__module__):
            feature = HANDLER_FEATURES.get(handler.handler_name)
            if feature and not self._on(feature):
                handler.enabled = False

    async def terminate(self):
        await self.browser.close()
        for task in list(self._background):
            task.cancel()

    # ------------------------------------------------------------ 公共

    def _on(self, feature: str) -> bool:
        return bool(section(self.config, feature).get("enable", True))

    def _cfg(self, feature: str) -> dict:
        return section(self.config, feature)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _llm_text(self, event: AstrMessageEvent, prompt: str, system_prompt: str, **kwargs) -> str:
        umo = event.unified_msg_origin
        provider_id = await self.context.get_current_chat_provider_id(umo)
        if not provider_id:
            raise RuntimeError("未配置对话模型")
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id, prompt=prompt, system_prompt=system_prompt, **kwargs
            )
        except TypeError:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id, prompt=prompt, system_prompt=system_prompt
            )
        return (resp.completion_text or "").strip()

    def _image_file(self, data: bytes, prefix: str = "omni") -> str:
        return self.temp.write(data, image_suffix(data), prefix)

    async def _render_html(self, tmpl: str, data: dict) -> str:
        return await self.html_render(tmpl, data, return_url=True)

    def _nycnm(self) -> tuple[str, float]:
        cfg = self._cfg("nycnm")
        return str(cfg.get("api_key") or ""), float(cfg.get("timeout") or 10)

    # ------------------------------------------------------------ LLM 请求钩子

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """关闭的功能不把函数工具交给 LLM；开启 Markdown 转图片时注入使用说明。"""
        # 会话里开始新的对话后，之前回退的内容不能再恢复，否则顺序会乱
        self.rewinder.drop(event.unified_msg_origin)
        tool_set = getattr(req, "func_tool", None)
        if tool_set is not None:
            for feature, names in FEATURE_TOOLS.items():
                if not self._on(feature):
                    for name in names:
                        tool_set.remove_tool(name)
        if self._on("markdown2img"):
            req.system_prompt = f"{req.system_prompt or ''}\n\n{markdown2img.INSTRUCTION}"

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """把回复里 <md>...</md> 包裹的内容渲染成图片。"""
        if not self._on("markdown2img"):
            return
        result = event.get_result()
        chain = getattr(result, "chain", None)
        if not chain or not any(isinstance(c, Comp.Plain) and "<md>" in (c.text or "") for c in chain):
            return
        new_chain = []
        for item in chain:
            if not (isinstance(item, Comp.Plain) and "<md>" in (item.text or "")):
                new_chain.append(item)
                continue
            for kind, content in markdown2img.split_markdown(item.text):
                if kind == "text":
                    new_chain.append(Comp.Plain(content))
                    continue
                path = self.temp.new_path(".png", "md")
                try:
                    await markdown2img.render(self.browser, content, path)
                    new_chain.append(Comp.Image.fromFileSystem(path))
                except Exception as exc:
                    logger.error(f"[Markdown 转图片] 渲染失败: {exc}")
                    new_chain.append(Comp.Plain(f"--- Markdown 渲染异常 ---\n{content}"))
        result.chain = new_chain

    # ------------------------------------------------------------ 任意消息：等待发图

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()
        first = text.split(maxsplit=1)[0].lower() if text else ""

        if first not in TAG_COMMANDS:
            async for result in self._tag_waiting(event):
                yield result
        if first not in QR_COMMANDS:
            async for result in self._qr_waiting(event):
                yield result

    # ------------------------------------------------------------ 娱乐小功能（原 meaning）

    @filter.command("左右脑互搏")
    async def left_brain(self, event: AstrMessageEvent, rounds: int = 3):
        """左右脑互搏，可指定回合数"""
        if not self._on("left_brain"):
            return
        max_rounds = max(1, int(self._cfg("left_brain").get("max_rounds", 10)))
        rounds = max(1, min(int(rounds), max_rounds))
        try:
            for i in range(rounds):
                name, prompt = fun.battle_prompt(i)
                text = await self._llm_text(event, "发动攻击：", prompt, temperature=1.5, max_tokens=50)
                yield event.plain_result(f"【{name}】\n{text}\n" + "═" * 20)
                await asyncio.sleep(1)
        except Exception as exc:
            yield event.plain_result(f"💥 战场崩溃：{exc}")
            return
        end_text, end_img = random.choice(fun.ENDINGS)
        chain = []
        try:
            status, _, data = await fetch(end_img, seconds=10)
            if status == 200 and data:
                chain.append(Comp.Image.fromFileSystem(self._image_file(data, "battle")))
        except Exception:
            pass
        chain.append(Comp.Plain(("" if chain else "🎲") + f"\n🏁 最终结果：{end_text}"))
        yield event.chain_result(chain)

    @filter.command("历史上的今天")
    async def history_today(self, event: AstrMessageEvent):
        """历史上的今天"""
        if not self._on("history_today"):
            return
        key, seconds = self._nycnm()
        try:
            kind, payload = await fun.history_today(
                key, str(self._cfg("history_today").get("format") or "image"), seconds
            )
        except Exception as exc:
            logger.warning(f"[历史上的今天] 获取失败: {exc}")
            yield event.plain_result("抱歉，暂时无法获取历史上的今天，请稍后再试。")
            return
        if kind == "image":
            yield event.image_result(self._image_file(payload, "history"))
        else:
            yield event.plain_result(payload)

    @filter.command("点阵字")
    async def dot_matrix(self, event: AstrMessageEvent):
        """点阵字 内容 填充字"""
        if not self._on("dot_matrix"):
            return
        parsed = fun.parse_dot_matrix(event.message_str or "")
        if not parsed:
            yield event.plain_result(fun.DOT_USAGE)
            return
        content, fill = parsed
        try:
            art = await asyncio.to_thread(fun.dot_matrix, content, fill)
        except Exception as exc:
            logger.error(f"[点阵字] 生成失败: {exc}")
            yield event.plain_result("抱歉，生成点阵字失败，请稍后再试。")
            return
        yield event.plain_result(f".\n{art}")

    @filter.command("看看腿")
    async def look_leg(self, event: AstrMessageEvent):
        """随机看看腿"""
        if not self._on("look_leg"):
            return
        try:
            data = await fun.look_leg(str(self._cfg("look_leg").get("api") or ""))
        except Exception as exc:
            logger.warning(f"[看看腿] 获取失败: {exc}")
            yield event.plain_result("抱歉，暂时无法获取图片，请稍后再试。")
            return
        yield event.image_result(self._image_file(data, "leg"))

    @filter.regex(r"^合成")
    async def emoji_mix(self, event: AstrMessageEvent):
        """合成两个 Emoji：/合成😀😭，可不加空格"""
        # 正则不像指令那样要求唤醒，这里补上：必须带前缀、@机器人或在私聊中
        if not event.is_at_or_wake_command or not self._on("emoji_mix"):
            return
        emojis = fun.parse_emoji_mix((event.message_str or "").strip())
        if emojis is None:
            return
        if len(emojis) != 2:
            yield event.plain_result(fun.EMOJI_USAGE)
            return
        try:
            url = await fun.emoji_mix(emojis[0], emojis[1])
        except Exception as exc:
            yield event.plain_result(str(exc) if "合成失败" in str(exc) else "Emoji 合成请求失败，请稍后再试")
            return
        try:
            status, _, data = await fetch(url, seconds=15)
            if status == 200 and data:
                yield event.image_result(self._image_file(data, "emoji"))
                return
        except Exception:
            pass
        yield event.plain_result(f"🎨 Emoji合成结果：\n{emojis[0]} + {emojis[1]} =\n查看合成图片：{url}")

    # ------------------------------------------------------------ 对话回退（原 llm_amnesia）

    @filter.command("rewind", alias={"回退"})
    async def rewind_cmd(self, event: AstrMessageEvent):
        """回退最近几轮对话：/回退 [轮数]，/rewind cancel 撤销"""
        if not self._on("rewind"):
            return
        arg = command_rest(event)
        if arg.lower() == "cancel":
            text = await self.rewinder.restore(self.context.conversation_manager, event.unified_msg_origin)
        else:
            rounds = rewind.parse_rounds(arg)
            if rounds is None:
                text = rewind.USAGE
            else:
                text = await self.rewinder.rewind(self.context.conversation_manager, event.unified_msg_origin, rounds)
        yield event.plain_result(text)

    @filter.command("取消回退")
    async def cancel_rewind(self, event: AstrMessageEvent):
        """恢复刚才回退掉的对话"""
        if not self._on("rewind"):
            return
        yield event.plain_result(
            await self.rewinder.restore(self.context.conversation_manager, event.unified_msg_origin)
        )

    # ------------------------------------------------------------ 今日人品

    @filter.command("今日人品")
    async def jrrp(self, event: AstrMessageEvent):
        """今日人品，每人每天固定，范围 1-100"""
        if not self._on("jrrp"):
            return
        yield event.plain_result(simple.jrrp(str(event.get_sender_id()), event.get_sender_name()))

    # ------------------------------------------------------------ 精确计算（原 primermath）

    @filter.llm_tool(name="calculate")
    async def calculate_tool(self, event: AstrMessageEvent, expression: str) -> str:
        """精确计算算式。用户问加减乘除、乘方、比较大小等算术问题时调用，不要自己心算。

        Args:
            expression(string): 算式，如 "123.4*5.6-7"、"2**10"、"3/7>0.4"；支持 + - * / // % **、括号、比较运算，以及 sqrt abs round log exp sin cos tan min max pi e
        """
        if not self._on("calculate"):
            return "精确计算工具已关闭"
        return simple.calculate(expression)

    # ------------------------------------------------------------ 浅草寺抽签

    def _sensoji_render(self, title: str, message: str) -> str:
        keep = int(self._cfg("sensoji").get("daily_cleanup", 1))
        return self.sensoji.render(title, message, keep)

    @filter.command("抽签")
    async def sensoji_draw(self, event: AstrMessageEvent):
        """浅草寺抽签，每天一签"""
        if not self._on("sensoji"):
            return
        result = self.sensoji.draw(event.get_sender_id())
        yield event.image_result(await asyncio.to_thread(self._sensoji_render, "抽签结果", result))

    @filter.command("转运")
    async def sensoji_change(self, event: AstrMessageEvent):
        """重新抽签，每日次数有限"""
        if not self._on("sensoji"):
            return
        cfg = self._cfg("sensoji")
        if not cfg.get("enable_change_fortune", True):
            path = await asyncio.to_thread(self._sensoji_render, "功能不可用", "当前管理员已禁用转运功能")
            yield event.image_result(path)
            return
        max_times = int(cfg.get("max_change_fortune_times", 3))
        status, result, _ = self.sensoji.change(event.get_sender_id(), max_times)
        if status == "limit":
            path = await asyncio.to_thread(self._sensoji_render, "转运失败", f"今日转运次数已达上限（{max_times}次）")
        else:
            path = await asyncio.to_thread(self._sensoji_render, "转运结果", result)
        yield event.image_result(path)

    async def _persona_prompt(self, umo: str) -> str:
        manager = getattr(self.context, "persona_manager", None)
        try:
            persona = await manager.get_default_persona_v3(umo) if manager else None
            return str((persona or {}).get("prompt", "") or "")
        except Exception:
            return ""

    async def _sensoji_explain(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        message = self.sensoji.today_result(event.get_sender_id()) or "今日尚未抽签"
        provider = self.context.get_using_provider(umo)
        if provider is None:
            yield event.plain_result("解签需要先配置对话模型")
            return
        contexts = []
        try:
            manager = self.context.conversation_manager
            cid = await manager.get_curr_conversation_id(umo)
            if cid:
                conversation = await manager.get_conversation(umo, cid)
                history = getattr(conversation, "history", None)
                contexts = json.loads(history) if isinstance(history, str) and history else (history or [])
        except Exception as exc:
            logger.debug(f"[抽签] 读取对话上下文失败: {exc}")
        try:
            resp = await provider.text_chat(
                prompt=EXPLAIN_PROMPT.format(message=message),
                contexts=contexts,
                system_prompt=await self._persona_prompt(umo),
            )
        except Exception as exc:
            logger.error(f"[抽签] 解签失败: {exc}")
            yield event.plain_result("解签失败，请稍后再试")
            return
        text = remove_escaped_emojis(resp.completion_text)
        yield event.image_result(await asyncio.to_thread(self._sensoji_render, "解签结果", text))

    @filter.command("解签")
    async def sensoji_explain(self, event: AstrMessageEvent):
        """解读今天抽到的签"""
        if not self._on("sensoji"):
            return
        async for result in self._sensoji_explain(event):
            yield result

    @filter.llm_tool("explain_fortune")
    async def explain_fortune_tool(self, event: AstrMessageEvent):
        """Explain the result of a fortune from Sensoji Temple.应当在`解签``解释一下抽的签`时被调用。"""
        if not self._on("sensoji"):
            return
        async for result in self._sensoji_explain(event):
            yield result

    # ------------------------------------------------------------ 卢恩占卜

    @filter.command("卢恩占卜")
    async def runes_divination(self, event: AstrMessageEvent):
        """卢恩占卜 [单符/三符/五符/九符/符文表] [问题]"""
        if not self._on("runes"):
            return
        args = command_rest(event).split(maxsplit=1)
        method = args[0].lower() if args else ""
        question = args[1].strip() if len(args) > 1 else "我想知道关于未来的指引"
        if not method:
            yield event.plain_result(runes.USAGE)
            return
        if method == "符文表":
            prompt, system = runes.table_request()
            yield event.request_llm(prompt=prompt, system_prompt=system)
            return
        if method not in runes.METHODS:
            yield event.plain_result(runes.INVALID)
            return
        cfg = self._cfg("runes")
        display, prompt, system = runes.divine(
            method, question, str(cfg.get("interpretation_depth", "详细")), bool(cfg.get("include_history", True))
        )
        yield event.plain_result(display)
        yield event.request_llm(prompt=prompt, system_prompt=system)

    # ------------------------------------------------------------ 梗图猜词

    def _gengtu_args(self) -> tuple[str, str, float]:
        key, seconds = self._nycnm()
        return str(self._cfg("gengtu").get("api_url") or ""), key, seconds

    @filter.command("梗图")
    async def gengtu_question(self, event: AstrMessageEvent):
        """梗图猜词：获取一道题"""
        if not self._on("gengtu"):
            return
        url, key, seconds = self._gengtu_args()
        if not key:
            yield event.plain_result("❌ 梗图猜词需要柠柚 API Key，请管理员在插件配置「柠柚 API」中填写")
            return
        try:
            question = await self.gengtu.fetch_question(url, key, seconds)
        except Exception as exc:
            logger.error(f"[梗图] 获取题目失败: {exc}")
            question = None
        if not question:
            yield event.plain_result("❌ 获取题目失败，请稍后重试")
            return
        qid, image_url = question
        self.gengtu.pending[sender_key(event)] = qid
        data = await self.gengtu.download_image(image_url, seconds)
        image = Comp.Image.fromFileSystem(self._image_file(data, "gengtu")) if data else Comp.Image.fromURL(image_url)
        yield event.chain_result([image, Comp.Plain("📝 发送 /答案 你的答案 作答\n💡 猜不出来就只发 /答案 查看答案")])

    @filter.command("答案")
    async def gengtu_answer(self, event: AstrMessageEvent):
        """梗图作答；不带内容时查看答案"""
        if not self._on("gengtu"):
            return
        key = sender_key(event)
        qid = self.gengtu.pending.get(key)
        if not qid:
            yield event.plain_result("ℹ️ 当前没有待作答的题目，请先发送 /梗图 获取题目")
            return
        url, api_key, seconds = self._gengtu_args()
        answer = command_rest(event)
        if not answer:
            try:
                correct_answer = await self.gengtu.hint(url, api_key, qid, seconds)
            except Exception as exc:
                logger.error(f"[梗图] 获取答案失败: {exc}")
                correct_answer = None
            if correct_answer:
                self.gengtu.pending.pop(key, None)
                yield event.plain_result(f"💡 正确答案：{correct_answer}\n再来一题：/梗图")
            else:
                yield event.plain_result("❌ 无法获取正确答案，请稍后重试")
            return
        try:
            message, correct, correct_answer = await self.gengtu.verify(url, api_key, qid, answer, seconds)
        except Exception as exc:
            logger.error(f"[梗图] 校验答案失败: {exc}")
            yield event.plain_result("❌ 校验失败，请稍后重试")
            return
        if correct is None:
            yield event.plain_result(message)
        elif correct:
            self.gengtu.pending.pop(key, None)
            yield event.plain_result("✅ 回答正确！" + (f"\n📘 正确答案：{correct_answer}" if correct_answer else ""))
        else:
            yield event.plain_result("❌ 回答不正确！再想想，或只发 /答案 查看答案")

    # ------------------------------------------------------------ 随机图片（Safebooru）

    @filter.command("随机图片")
    async def random_image(self, event: AstrMessageEvent):
        """随机图片 [标签]：不带标签完全随机，标签支持中文"""
        if not self._on("safebooru"):
            return
        tag = command_rest(event)
        cfg = self._cfg("safebooru")
        try:
            if tag:
                post = await self.safebooru.pick(tag, int(cfg.get("limit", 100)))
                url, tags = post["file_url"], str(post.get("tags", ""))
            else:
                url, tags = await self.safebooru.random_image()
            status, _, data = await fetch(url, seconds=60)
            if status != 200 or not data:
                raise RuntimeError("下载图片失败，请稍后重试。")
        except Exception as exc:
            yield event.plain_result(str(exc) or "获取图片失败，请稍后重试。")
            return
        if cfg.get("display_tags", False):
            yield event.plain_result("Tags: " + tags)
        yield event.image_result(self._image_file(data, "safebooru"))

    # ------------------------------------------------------------ 图片标签识别

    async def _run_tagger(self, event: AstrMessageEvent, image):
        cfg = self._cfg("tagger")
        try:
            data = await image_bytes(image)
            text = await tagger.analyze(
                data,
                api_url=str(cfg.get("api_url") or tagger.DEFAULT_API_URL),
                model=str(cfg.get("model") or tagger.DEFAULT_MODEL),
                general_threshold=float(cfg.get("general_threshold", 0.35)),
                character_threshold=float(cfg.get("character_threshold", 0.85)),
            )
        except Exception as exc:
            text = f"❌ 处理过程中出现错误\n错误信息：{exc}"
        yield event.plain_result(text)

    @filter.command("tag")
    async def tag(self, event: AstrMessageEvent):
        """识别图片 tag：带图或引用图片发送，或发送后再发图"""
        if not self._on("tagger"):
            return
        images = message_images(event)
        if images:
            self.tag_waiter.cancel(event)
            yield event.plain_result("✅ 已收到图片\n正在分析中...")
            async for result in self._run_tagger(event, images[0]):
                yield result
            return
        seconds = int(self._cfg("tagger").get("wait_seconds", 60))
        self.tag_waiter.start(event, seconds)
        yield event.plain_result(f"{event.get_sender_name()}，请在{seconds}秒内发送一张图片，我将识别图像标签喵~")

    async def _tag_waiting(self, event: AstrMessageEvent):
        state = self.tag_waiter.state(event)
        if state == "none" or not self._on("tagger"):
            return
        images = message_images(event)
        if not images:
            return
        if state == "expired":
            yield event.plain_result("❌ 超时了，请重新发送 /tag")
            return
        self.tag_waiter.cancel(event)
        yield event.plain_result("✅ 已收到图片\n正在分析中...")
        async for result in self._run_tagger(event, images[0]):
            yield result

    # ------------------------------------------------------------ 二维码

    async def _decode_and_reply(self, event: AstrMessageEvent, image):
        try:
            data = await image_bytes(image)
            results = await asyncio.to_thread(qrcode_tool.decode_qr, data)
        except Exception as exc:
            logger.error(f"[二维码] 解析失败: {exc}")
            yield event.plain_result("❌ 解析过程中发生错误")
            return
        yield event.plain_result(qrcode_tool.format_results(results))

    @filter.command("二维码", alias={"扫码"})
    async def qrcode(self, event: AstrMessageEvent):
        """二维码：带内容生成，带图（或引用图片）识别成链接"""
        if not self._on("qrcode"):
            return
        images = message_images(event)
        if images:
            self.qr_waiter.cancel(event)
            async for result in self._decode_and_reply(event, images[0]):
                yield result
            return
        text = command_rest(event)
        if text:
            try:
                data = await asyncio.to_thread(qrcode_tool.make_qr, text)
            except Exception as exc:
                logger.error(f"[二维码] 生成失败: {exc}")
                yield event.plain_result("❌ 二维码生成失败，请检查内容")
                return
            yield event.image_result(self._image_file(data, "qr"))
            return
        seconds = int(self._cfg("qrcode").get("wait_seconds", 60))
        self.qr_waiter.start(event, seconds)
        yield event.plain_result(qrcode_tool.WAIT_TEXT.format(seconds=seconds))

    async def _qr_waiting(self, event: AstrMessageEvent):
        state = self.qr_waiter.state(event)
        if state == "none" or not self._on("qrcode"):
            return
        images = message_images(event)
        if not images:
            return
        if state == "expired":
            yield event.plain_result("❌ 超时了，请重新发送 /二维码")
            return
        self.qr_waiter.cancel(event)
        async for result in self._decode_and_reply(event, images[0]):
            yield result

    # ------------------------------------------------------------ 天气

    @property
    def api_key(self) -> str:
        """供 decision 等插件读取的心知天气 Key；天气功能关闭时视为未配置。"""
        if not self._on("weather"):
            return ""
        return str(self._cfg("weather").get("seniverse_api_key") or "")

    @property
    def default_city(self) -> str:
        return str(self._cfg("weather").get("default_city") or "上海")

    async def get_current_weather_by_city(self, city: str):
        return await weather.current(self.api_key, city)

    async def get_forecast_weather_by_city(self, city: str):
        return await weather.forecast(self.api_key, city)

    async def _weather(self, event: AstrMessageEvent, city: str | None):
        city = (city or "").strip() or self.default_city
        if not self.api_key:
            yield event.plain_result(weather.NO_KEY)
            return
        now, days, tips = await asyncio.gather(
            weather.current(self.api_key, city),
            weather.forecast(self.api_key, city),
            weather.suggestions(self.api_key, city),
        )
        if now is None and not days:
            yield event.plain_result(f"查询 [{city}] 的天气失败，请检查城市名或稍后再试。")
            return
        if self._cfg("weather").get("send_mode", "text") == "image":
            chain = []
            if now:
                chain.append(Comp.Image.fromURL(await weather.render_current(self._render_html, now)))
            if days:
                chain.append(Comp.Image.fromURL(await weather.render_forecast(self._render_html, city, days, tips)))
            yield event.chain_result(chain)
        else:
            parts = []
            if now:
                parts.append(weather.current_text(now))
            if days:
                parts.append(weather.forecast_text(city, days, tips))
            yield event.plain_result("\n\n".join(parts).strip())

    @filter.command("天气")
    async def weather_cmd(self, event: AstrMessageEvent):
        """天气 [城市]：实况、未来 3 天预报和生活指数"""
        if not self._on("weather"):
            return
        async for result in self._weather(event, command_rest(event)):
            yield result

    @filter.llm_tool(name="get_weather")
    async def get_weather_tool(self, event: AstrMessageEvent, city: str) -> MessageEventResult:
        """查询城市天气（实况、未来 3 天预报和生活指数），用户问某地天气或要不要带伞等时调用。

        Args:
            city(string): 地点，例如：杭州，或者浙江杭州
        """
        if not self._on("weather"):
            return
        async for result in self._weather(event, city):
            yield result

    # ------------------------------------------------------------ 百度翻译

    @filter.command("翻译")
    async def translate_cmd(self, event: AstrMessageEvent):
        """翻译 <目标语种> <内容>"""
        if not self._on("translation"):
            return
        parts = command_rest(event).split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result(translation.help_text())
            return
        cfg = self._cfg("translation")
        try:
            text = await translation.translate(
                str(cfg.get("appid") or ""), str(cfg.get("secret_key") or ""), parts[0], parts[1].strip()
            )
        except Exception as exc:
            text = f"调用翻译 API 时出错: {exc}"
        yield event.plain_result(text)

    # ------------------------------------------------------------ IP / 域名查询

    @filter.command("查IP", alias={"查ip"})
    async def ip_query(self, event: AstrMessageEvent):
        """查IP <IP或域名>：归属地、运营商"""
        if not self._on("ipquery"):
            return
        target = command_rest(event).split(maxsplit=1)
        yield event.plain_result(await ipquery.fetch_ip_info(target[0]) if target else ipquery.IP_USAGE)

    @filter.command("查域名")
    async def whois_query(self, event: AstrMessageEvent):
        """查域名 <域名>：Whois 注册信息"""
        if not self._on("ipquery"):
            return
        target = command_rest(event).split(maxsplit=1)
        yield event.plain_result(await ipquery.fetch_whois_info(target[0]) if target else ipquery.WHOIS_USAGE)

    # ------------------------------------------------------------ B 站查询（AICU）

    @filter.command("B站查询", alias={"b站查询"})
    async def aicu_uid(self, event: AstrMessageEvent):
        """B站查询 <UID>：用户评论画像"""
        if not self._on("aicu"):
            return
        args = command_rest(event).split(maxsplit=1)
        uid = args[0] if args else ""
        if not uid.isdigit():
            yield event.plain_result("❌ 请输入纯数字 UID，例如 /B站查询 2")
            return
        yield event.plain_result(f"🔍 正在获取 UID: {uid} 的数据...")
        try:
            path = await aicu.analyze(
                self.browser, uid, str(self._cfg("aicu").get("cookie") or ""), self.data_dir / "tmp"
            )
        except Exception as exc:
            logger.error(f"[AICU] 处理失败: {exc}", exc_info=True)
            yield event.plain_result("❌ 插件运行错误，请查看后台日志。")
            return
        if path is None:
            yield event.plain_result("❌ 数据获取失败。请检查配置中的 Cookie 是否正确。")
            return
        yield event.image_result(path)

    # ------------------------------------------------------------ 数学绘图（仅函数工具）

    def _math_core(self):
        if self._math is None:
            from .omni.math_plotter import MathPlotterCore

            plots_dir = str(self.data_dir / "plots")
            self._math = MathPlotterCore(lambda key: self._cfg("math_plotter").get(key), plots_dir)
        return self._math

    def _cleanup_plots(self, max_age_days: int = 3) -> None:
        plots = self.data_dir / "plots"
        deadline = time.time() - max_age_days * 86400
        for file in plots.glob("*.png") if plots.exists() else []:
            try:
                if file.stat().st_mtime < deadline:
                    file.unlink()
            except OSError:
                pass

    def _math_ready(self):
        if not self._on("math_plotter"):
            return None
        self._cleanup_plots()
        return self._math_core()

    @filter.llm_tool(name="plot_2d")
    async def plot_2d(
        self, event: AstrMessageEvent, type: str, expression: str, x_range: str = "", y_range: str = "",
        t_range: str = "", title: str = "", xlabel: str = "", ylabel: str = "",
    ) -> str:
        """绘制二维数学图像并直接发给用户。讲解函数、方程、曲线时调用。

        Args:
            type(string): 图像类型：function（y=f(x)，可多条对比）、implicit（隐式方程 F(x,y)=0）、polar（极坐标 r=f(theta)）、parametric（参数方程）、vector_field（二维矢量场）
            expression(string): 表达式，多个用分号分隔。function 如 "sin(x)" 或 "sin(x);x**2"；implicit 如 "x**2+y**2=1"；polar 如 "1+cos(theta)"；parametric 为 "x(t);y(t)" 如 "cos(t);sin(t)"；vector_field 为 "Fx;Fy" 如 "-y;x"。支持 sin cos tan exp log sqrt abs pi
            x_range(string): x 范围 "最小,最大"，如 "-10,10"，可留空
            y_range(string): y 范围，implicit 和 vector_field 使用，可留空
            t_range(string): parametric 的 t 范围或 polar 的 theta 范围，可留空
            title(string): 标题，可留空
            xlabel(string): x 轴标签，可留空
            ylabel(string): y 轴标签，可留空
        """
        core = self._math_ready()
        if core is None:
            return "数学绘图功能已关闭"
        return await plot_tools.plot_2d(core, event, type, expression, x_range, y_range, t_range, title, xlabel, ylabel)

    @filter.llm_tool(name="plot_3d")
    async def plot_3d(
        self, event: AstrMessageEvent, type: str, expression: str, x_range: str = "", y_range: str = "",
        z_range: str = "", t_range: str = "", phi_range: str = "", title: str = "",
        xlabel: str = "", ylabel: str = "", zlabel: str = "",
    ) -> str:
        """绘制三维数学图像并直接发给用户。讲解曲面、空间曲线时调用。

        Args:
            type(string): 图像类型：surface（曲面 z=f(x,y)，可多个对比）、implicit（隐式曲面 F(x,y,z)=0）、spherical（球坐标 r=f(theta,phi)）、parametric（空间参数曲线）
            expression(string): 表达式，多个用分号分隔。surface 如 "x**2+y**2" 或 "x**2+y**2;sqrt(x**2+y**2)"；implicit 如 "x**2+y**2+z**2=1"；spherical 如 "1+cos(theta)"；parametric 为 "x(t);y(t);z(t)" 如 "cos(t);sin(t);t/5"
            x_range(string): x 范围 "最小,最大"，可留空
            y_range(string): y 范围，可留空
            z_range(string): z 范围，implicit 使用，可留空
            t_range(string): parametric 的 t 范围或 spherical 的 theta 范围，可留空
            phi_range(string): spherical 的 phi 范围，可留空
            title(string): 标题，可留空
            xlabel(string): x 轴标签，可留空
            ylabel(string): y 轴标签，可留空
            zlabel(string): z 轴标签，可留空
        """
        core = self._math_ready()
        if core is None:
            return "数学绘图功能已关闭"
        return await plot_tools.plot_3d(
            core, event, type, expression, x_range, y_range, z_range, t_range, phi_range,
            title, xlabel, ylabel, zlabel,
        )

    @filter.llm_tool(name="plot_vectors")
    async def plot_vectors(
        self, event: AstrMessageEvent, type: str, vectors: str, scalar: float = 2.0, title: str = "",
        xlabel: str = "", ylabel: str = "", zlabel: str = "",
    ) -> str:
        """绘制向量示意图并直接发给用户。讲解向量、向量运算、基变换时调用。

        Args:
            type(string): 类型：2d（二维向量）、3d（三维向量）、addition（向量加法）、scaling（向量数乘）、basis（基变换）
            vectors(string): 2d/3d 为向量列表，用分号分隔，每个写 "x,y" 或 "起点->终点"，可加 ":颜色:标签"，如 "3,4:red:a;0,0->2,3:blue:b"；addition 写两个向量 "1,2;3,1"；scaling 写一个向量 "1,3"；basis 写新基 "(1,1),(1,-1)"
            scalar(number): scaling 的数乘系数，默认 2
            title(string): 标题，可留空
            xlabel(string): x 轴标签，可留空
            ylabel(string): y 轴标签，可留空
            zlabel(string): z 轴标签，3d 使用，可留空
        """
        core = self._math_ready()
        if core is None:
            return "数学绘图功能已关闭"
        return await plot_tools.plot_vectors(core, event, type, vectors, scalar, title, xlabel, ylabel, zlabel)

    # ------------------------------------------------------------ 点赞（仅 aiocqhttp）

    def _zanwo_cfg(self) -> dict:
        cfg = self.config.get("zanwo")
        if not isinstance(cfg, dict):
            cfg = {}
            self.config["zanwo"] = cfg
        cfg.setdefault("white_list_groups", [])
        cfg.setdefault("subscribed_users", [])
        return cfg

    async def _auto_like_subscribed(self, client) -> None:
        cfg = self._zanwo_cfg()
        today = datetime.now().strftime("%Y-%m-%d")
        users = [str(u) for u in cfg.get("subscribed_users") or []]
        if not users or cfg.get("zanwo_date") == today:
            return
        cfg["zanwo_date"] = today
        self.config.save_config()
        try:
            await zanwo.like_users(client, users)
        except Exception as exc:
            logger.warning(f"[点赞] 自动点赞订阅用户失败: {exc}")

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.regex(r"^赞.*")
    async def like_me(self, event: AstrMessageEvent):
        """发送“赞我”或“赞@某人”点赞"""
        if not self._on("zanwo"):
            return
        cfg = self._zanwo_cfg()
        group_id = str(event.get_group_id() or "")
        allowed = {str(g) for g in cfg.get("white_list_groups") or []}
        if group_id and allowed and group_id not in allowed:
            return
        if (event.message_str or "").strip() == "赞我":
            targets = [str(event.get_sender_id())]
        else:
            self_id = str(event.get_self_id())
            targets = [
                str(seg.qq) for seg in event.get_messages()
                if isinstance(seg, Comp.At) and str(seg.qq) not in (self_id, "all")
            ]
        if not targets:
            return
        client = event.bot
        yield event.plain_result(await zanwo.like_users(client, targets))
        self._spawn(self._auto_like_subscribed(client))

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("订阅点赞")
    async def subscribe_like(self, event: AstrMessageEvent):
        """订阅每日自动点赞"""
        if not self._on("zanwo"):
            return
        cfg = self._zanwo_cfg()
        sender = str(event.get_sender_id())
        users = [str(u) for u in cfg.get("subscribed_users") or []]
        if sender in users:
            yield event.plain_result("你已经订阅点赞了哦~")
            return
        cfg["subscribed_users"] = users + [sender]
        self.config.save_config()
        yield event.plain_result("订阅成功！我将每天自动为你点赞")

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("取消订阅点赞")
    async def unsubscribe_like(self, event: AstrMessageEvent):
        """取消每日自动点赞"""
        if not self._on("zanwo"):
            return
        cfg = self._zanwo_cfg()
        sender = str(event.get_sender_id())
        users = [str(u) for u in cfg.get("subscribed_users") or []]
        if sender not in users:
            yield event.plain_result("你还没有订阅点赞哦~")
            return
        cfg["subscribed_users"] = [u for u in users if u != sender]
        self.config.save_config()
        yield event.plain_result("已取消订阅！我将不再自动给你点赞")
