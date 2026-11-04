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
    qrcode_tool,
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
    setting,
)
from .omni.gengtu import HELP_TEXT as GENGTU_HELP
from .omni.gengtu import Gengtu
from .omni.safebooru import Safebooru
from .omni.sensoji import EXPLAIN_PROMPT, Sensoji, remove_escaped_emojis

PRIMERMATH_TOOLS = ("FFRadd", "FFRminus", "FFRdid", "FFRmul", "FFRcomp")
MATH_TOOLS = (
    "plot_function", "plot_multiple", "plot_implicit", "plot_3d_function", "plot_polar",
    "plot_parametric", "plot_3d_spherical", "plot_vector_field_2d", "plot_3d_multiple",
    "plot_implicit_3d", "plot_3d_parametric", "plot_vector_2d", "plot_vector_3d",
    "plot_vector_addition", "plot_vector_scaling", "plot_basis_transform",
)
FEATURE_TOOLS = {
    "primermath": PRIMERMATH_TOOLS,
    "math_plotter": MATH_TOOLS,
    "sensoji": ("explain_fortune",),
    "weather": ("get_current_weather", "get_forecast_weather"),
}
TAG_COMMANDS = {"tag"}
QR_DECODE_COMMANDS = {"qr_decode", "二维码转链接", "扫码", "识别二维码", "解析二维码"}


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
        self._math = None
        self._background: set[asyncio.Task] = set()

    async def terminate(self):
        await self.browser.close()
        for task in list(self._background):
            task.cancel()

    # ------------------------------------------------------------ 公共

    def _on(self, feature: str) -> bool:
        return bool(self.config.get(f"enable_{feature}", True))

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

    # ------------------------------------------------------------ LLM 请求钩子

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """关闭的功能不把函数工具交给 LLM；开启 Markdown 转图片时注入使用说明。"""
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

    # ------------------------------------------------------------ 任意消息：关键词与等待发图

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        text = (event.message_str or "").strip()
        first = text.split(maxsplit=1)[0].lower() if text else ""

        if first not in TAG_COMMANDS:
            async for result in self._tag_waiting(event):
                yield result
        if first not in QR_DECODE_COMMANDS:
            async for result in self._qr_waiting(event):
                yield result

        if not text:
            return
        if "历史上的今天" in text and self._on("history_today"):
            async for result in self._history_today(event):
                yield result
        elif "点阵字" in text and self._on("dot_matrix"):
            async for result in self._dot_matrix(event, text):
                yield result
        elif "看看腿" in text and self._on("look_leg"):
            async for result in self._look_leg(event):
                yield result
        elif text.startswith("合成") and self._on("emoji_mix"):
            async for result in self._emoji_mix(event, text):
                yield result

    # ------------------------------------------------------------ 娱乐小功能（原 meaning）

    @filter.command("左右脑互搏")
    async def left_brain(self, event: AstrMessageEvent, rounds: int = 3):
        """左右脑互搏，可指定回合数"""
        if not self._on("left_brain"):
            return
        max_rounds = max(1, int(setting(self.config, "fun", "left_brain_max_rounds", 10)))
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

    async def _history_today(self, event: AstrMessageEvent):
        nycnm = section(self.config, "nycnm")
        try:
            kind, payload = await fun.history_today(
                str(nycnm.get("api_key") or ""),
                str(setting(self.config, "fun", "history_format", "image")),
                float(nycnm.get("timeout") or 10),
            )
        except Exception as exc:
            logger.warning(f"[历史上的今天] 获取失败: {exc}")
            yield event.plain_result("抱歉，暂时无法获取历史上的今天，请稍后再试。")
            return
        if kind == "image":
            yield event.image_result(self._image_file(payload, "history"))
        else:
            yield event.plain_result(payload)

    async def _dot_matrix(self, event: AstrMessageEvent, text: str):
        parsed = fun.parse_dot_matrix(text)
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

    async def _look_leg(self, event: AstrMessageEvent):
        try:
            data = await fun.look_leg(str(setting(self.config, "fun", "look_leg_api", "")))
        except Exception as exc:
            logger.warning(f"[看看腿] 获取失败: {exc}")
            yield event.plain_result("抱歉，暂时无法获取图片，请稍后再试。")
            return
        yield event.image_result(self._image_file(data, "leg"))

    async def _emoji_mix(self, event: AstrMessageEvent, text: str):
        emojis = fun.extract_emojis(text[2:])
        if len(emojis) < 2:
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

    # ------------------------------------------------------------ 今日人品

    @filter.command("jrrp")
    async def jrrp(self, event: AstrMessageEvent):
        """今日人品值查询，每个用户每天固定，范围1-100"""
        if not self._on("jrrp"):
            return
        yield event.plain_result(simple.jrrp(str(event.get_sender_id()), event.get_sender_name()))

    # ------------------------------------------------------------ 四则运算工具（原 primermath）

    async def _arith(self, op: str, num1: float, num2: float) -> str:
        if not self._on("primermath"):
            return "四则运算工具已关闭"
        return simple.arithmetic(op, num1, num2)

    @filter.llm_tool(name="FFRadd")
    async def FFRadd(self, event: AstrMessageEvent, num1: float, num2: float) -> str:
        """当用户询问两个数的和时

        Args:
            num1(number): 第一个数字
            num2(number): 第二个数字
        """
        return await self._arith("add", num1, num2)

    @filter.llm_tool(name="FFRminus")
    async def FFRminus(self, event: AstrMessageEvent, num1: float, num2: float) -> str:
        """当用户询问两个数的差时

        Args:
            num1(number): 第一个数字
            num2(number): 第二个数字
        """
        return await self._arith("minus", num1, num2)

    @filter.llm_tool(name="FFRdid")
    async def FFRdid(self, event: AstrMessageEvent, num1: float, num2: float) -> str:
        """当用户询问两个数的商时

        Args:
            num1(number): 第一个数字
            num2(number): 第二个数字
        """
        return await self._arith("div", num1, num2)

    @filter.llm_tool(name="FFRmul")
    async def FFRmul(self, event: AstrMessageEvent, num1: float, num2: float) -> str:
        """当用户询问两个数的乘积时

        Args:
            num1(number): 第一个数字
            num2(number): 第二个数字
        """
        return await self._arith("mul", num1, num2)

    @filter.llm_tool(name="FFRcomp")
    async def FFRcomp(self, event: AstrMessageEvent, num1: float, num2: float) -> str:
        """当用户询问两个数的大小时

        Args:
            num1(number): 第一个数字
            num2(number): 第二个数字
        """
        return await self._arith("comp", num1, num2)

    # ------------------------------------------------------------ 浅草寺抽签

    def _sensoji_render(self, title: str, message: str) -> str:
        keep = int(setting(self.config, "sensoji", "daily_cleanup", 1))
        return self.sensoji.render(title, message, keep)

    @filter.command("抽签帮助")
    async def sensoji_help(self, event: AstrMessageEvent):
        """显示抽签帮助"""
        if not self._on("sensoji"):
            return
        cfg = section(self.config, "sensoji")
        max_times = int(cfg.get("max_change_fortune_times", 3))
        if cfg.get("enable_change_fortune", True):
            change = f"3. 转运 - 重新抽取签文(每日限{'∞' if max_times == 0 else max_times}次)"
        else:
            change = "3. 转运 - (功能已禁用)"
        yield event.plain_result(f"浅草寺抽签使用说明：\n\n1. 抽签 - 每日抽取一次签文\n2. 解签 - 解读今日抽到的签文\n{change}")

    @filter.command("抽签")
    async def sensoji_draw(self, event: AstrMessageEvent):
        """浅草寺抽签"""
        if not self._on("sensoji"):
            return
        result = self.sensoji.draw(event.get_sender_id())
        yield event.image_result(await asyncio.to_thread(self._sensoji_render, "抽签结果", result))

    @filter.command("转运")
    async def sensoji_change(self, event: AstrMessageEvent):
        """浅草寺转运"""
        if not self._on("sensoji"):
            return
        cfg = section(self.config, "sensoji")
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
        """LLM 解签"""
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
        """卢恩符文占卜：卢恩占卜 [单符/三符/五符/九符/符文表] [问题]"""
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
        cfg = section(self.config, "runes")
        display, prompt, system = runes.divine(
            method, question, str(cfg.get("interpretation_depth", "详细")), bool(cfg.get("include_history", True))
        )
        yield event.plain_result(display)
        yield event.request_llm(prompt=prompt, system_prompt=system)

    # ------------------------------------------------------------ 梗图猜词

    def _gengtu_args(self) -> tuple[str, str, float]:
        nycnm = section(self.config, "nycnm")
        url = str(setting(self.config, "gengtu", "api_url", "") or "")
        return url, str(nycnm.get("api_key") or ""), float(nycnm.get("timeout") or 10)

    @filter.command("gengtu", alias={"梗图", "抽象猜词", "猜词"})
    async def gengtu_question(self, event: AstrMessageEvent):
        """获取梗图题目"""
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
        yield event.chain_result([image, Comp.Plain("📝 使用 /答案 你的答案 作答\n💡 查看提示：/提示 或 /hint")])

    @filter.command("answer", alias={"答案", "gengtu_answer", "猜词答案"})
    async def gengtu_answer(self, event: AstrMessageEvent):
        """提交梗图答案"""
        if not self._on("gengtu"):
            return
        answer = command_rest(event)
        if not answer:
            yield event.plain_result("❌ 用法错误！请使用：/答案 你的答案")
            return
        key = sender_key(event)
        qid = self.gengtu.pending.get(key)
        if not qid:
            yield event.plain_result("ℹ️ 当前没有待作答的题目，请先使用 /梗图 获取题目")
            return
        url, api_key, seconds = self._gengtu_args()
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
            yield event.plain_result("❌ 回答不正确！\n💡 如需查看正确答案，请使用 /提示 命令")

    @filter.command("hint", alias={"提示", "答案提示", "正确答案"})
    async def gengtu_hint(self, event: AstrMessageEvent):
        """查看当前梗图的正确答案"""
        if not self._on("gengtu"):
            return
        qid = self.gengtu.pending.get(sender_key(event))
        if not qid:
            yield event.plain_result("ℹ️ 当前没有待作答的题目，请先使用 /梗图 获取题目")
            return
        url, key, seconds = self._gengtu_args()
        try:
            answer = await self.gengtu.hint(url, key, qid, seconds)
        except Exception as exc:
            logger.error(f"[梗图] 获取提示失败: {exc}")
            answer = None
        if answer:
            yield event.plain_result(f"💡 正确答案：{answer}\n📝 请使用 /答案 {answer} 来完成此题目")
        else:
            yield event.plain_result("❌ 无法获取正确答案，请稍后重试")

    @filter.command("help_gengtu", alias={"梗图帮助", "猜词帮助", "使用说明"})
    async def gengtu_help(self, event: AstrMessageEvent):
        """梗图猜词帮助"""
        if not self._on("gengtu"):
            return
        yield event.plain_result(GENGTU_HELP)

    # ------------------------------------------------------------ Safebooru

    @filter.command("safebooru")
    async def safebooru_search(self, event: AstrMessageEvent):
        """按标签获取 safebooru 图片（支持中文标签）"""
        if not self._on("safebooru"):
            return
        tag = command_rest(event)
        if not tag:
            yield event.plain_result("用法：/safebooru <标签>，例如 /safebooru 猫耳")
            return
        cfg = section(self.config, "safebooru")
        try:
            post = await self.safebooru.pick(tag, int(cfg.get("limit", 100)))
            status, _, data = await fetch(post["file_url"], seconds=60)
            if status != 200 or not data:
                raise RuntimeError("下载图片失败。")
        except Exception as exc:
            yield event.plain_result(str(exc) or "获取图片失败，请稍后重试。")
            return
        if cfg.get("display_tags", False):
            yield event.plain_result("Tags: " + str(post.get("tags", "")))
        yield event.image_result(self._image_file(data, "safebooru"))

    @filter.command("safebooru_random")
    async def safebooru_random(self, event: AstrMessageEvent):
        """获取一张 safebooru 随机图片"""
        if not self._on("safebooru"):
            return
        try:
            src, alt = await self.safebooru.random_image()
            status, _, data = await fetch(src, seconds=60)
        except Exception as exc:
            yield event.plain_result(str(exc) or "请求随机图片失败，请稍后重试。")
            return
        if section(self.config, "safebooru").get("display_tags", False):
            yield event.plain_result("Tags: " + alt)
        if status == 200 and data:
            yield event.image_result(self._image_file(data, "safebooru"))
        else:
            yield event.image_result(src)

    # ------------------------------------------------------------ 图片标签识别

    async def _run_tagger(self, event: AstrMessageEvent, image):
        cfg = section(self.config, "tagger")
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
        """识别图片 tag：带图或引用图片发送 /tag，或发送 /tag 后再发图"""
        if not self._on("tagger"):
            return
        images = message_images(event)
        if images:
            self.tag_waiter.cancel(event)
            yield event.plain_result("✅ 已收到图片\n正在分析中...")
            async for result in self._run_tagger(event, images[0]):
                yield result
            return
        seconds = int(setting(self.config, "tagger", "wait_seconds", 60))
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
            yield event.plain_result("❌ 超时了，请重新发送 /tag 命令")
            return
        self.tag_waiter.cancel(event)
        yield event.plain_result("✅ 已收到图片\n正在分析中...")
        async for result in self._run_tagger(event, images[0]):
            yield result

    # ------------------------------------------------------------ 二维码

    @filter.command("qr")
    async def qr_generate(self, event: AstrMessageEvent):
        """将链接或文字转换为二维码：/qr <链接>"""
        if not self._on("qrcode"):
            return
        text = command_rest(event)
        if not text:
            yield event.plain_result(qrcode_tool.GEN_USAGE)
            return
        try:
            data = await asyncio.to_thread(qrcode_tool.make_qr, text)
        except Exception as exc:
            logger.error(f"[二维码] 生成失败: {exc}")
            yield event.plain_result("❌ 二维码生成失败，请检查内容")
            return
        yield event.image_result(self._image_file(data, "qr"))

    async def _decode_and_reply(self, event: AstrMessageEvent, image):
        try:
            data = await image_bytes(image)
            results = await asyncio.to_thread(qrcode_tool.decode_qr, data)
        except Exception as exc:
            logger.error(f"[二维码] 解析失败: {exc}")
            yield event.plain_result("❌ 解析过程中发生错误")
            return
        yield event.plain_result(qrcode_tool.format_results(results))

    @filter.command("qr_decode", alias={"二维码转链接", "扫码", "识别二维码", "解析二维码"})
    async def qr_decode(self, event: AstrMessageEvent):
        """二维码转链接：带图或引用图片发送，或发送指令后再发图"""
        if not self._on("qrcode"):
            return
        images = message_images(event)
        if images:
            self.qr_waiter.cancel(event)
            async for result in self._decode_and_reply(event, images[0]):
                yield result
            return
        seconds = int(setting(self.config, "qrcode", "wait_seconds", 60))
        self.qr_waiter.start(event, seconds)
        yield event.plain_result(qrcode_tool.DECODE_USAGE.format(seconds=seconds))

    async def _qr_waiting(self, event: AstrMessageEvent):
        state = self.qr_waiter.state(event)
        if state == "none" or not self._on("qrcode"):
            return
        images = message_images(event)
        if not images:
            return
        if state == "expired":
            yield event.plain_result("❌ 超时了，请重新发送 /qr_decode")
            return
        self.qr_waiter.cancel(event)
        async for result in self._decode_and_reply(event, images[0]):
            yield result

    # ------------------------------------------------------------ 天气

    @property
    def api_key(self) -> str:
        """兼容旧天气插件的属性（供其他插件读取），天气功能关闭时视为未配置。"""
        if not self._on("weather"):
            return ""
        return str(setting(self.config, "weather", "seniverse_api_key", "") or "")

    @property
    def default_city(self) -> str:
        return str(setting(self.config, "weather", "default_city", "上海") or "上海")

    async def get_current_weather_by_city(self, city: str):
        return await weather.current(self.api_key, city)

    async def get_forecast_weather_by_city(self, city: str):
        return await weather.forecast(self.api_key, city)

    async def _weather_current(self, event: AstrMessageEvent, city: str | None):
        city = (city or "").strip() or self.default_city
        if not self.api_key:
            yield event.plain_result(weather.NO_KEY)
            return
        data = await weather.current(self.api_key, city)
        if data is None:
            yield event.plain_result(f"查询 [{city}] 的当前天气失败，请稍后再试。")
            return
        if setting(self.config, "weather", "send_mode", "text") == "image":
            yield event.image_result(await weather.render_current(self._render_html, data))
        else:
            yield event.plain_result(weather.current_text(data))

    async def _weather_forecast(self, event: AstrMessageEvent, city: str | None):
        city = (city or "").strip() or self.default_city
        if not self.api_key:
            yield event.plain_result(weather.NO_KEY)
            return
        days = await weather.forecast(self.api_key, city)
        if not days:
            yield event.plain_result(f"查询 [{city}] 的未来天气失败，请稍后再试。")
            return
        tips = await weather.suggestions(self.api_key, city)
        if setting(self.config, "weather", "send_mode", "text") == "image":
            yield event.image_result(await weather.render_forecast(self._render_html, city, days, tips))
        else:
            yield event.plain_result(weather.forecast_text(city, days, tips))

    @filter.command_group("weather")
    def weather_group(self):
        """天气查询：/weather current|forecast|help <城市>"""

    @weather_group.command("current")
    async def weather_current(self, event: AstrMessageEvent, city: str = ""):
        """查看当前实况天气：/weather current <城市>"""
        if not self._on("weather"):
            return
        async for result in self._weather_current(event, city):
            yield result

    @weather_group.command("forecast")
    async def weather_forecast(self, event: AstrMessageEvent, city: str = ""):
        """查看未来3天天气预报及生活指数：/weather forecast <城市>"""
        if not self._on("weather"):
            return
        async for result in self._weather_forecast(event, city):
            yield result

    @weather_group.command("help")
    async def weather_help(self, event: AstrMessageEvent):
        """天气帮助"""
        if not self._on("weather"):
            return
        yield event.plain_result(weather.HELP_TEXT)

    @filter.llm_tool(name="get_current_weather")
    async def get_current_weather_tool(self, event: AstrMessageEvent, city: str) -> MessageEventResult:
        """当用户对某个城市的天气情况感兴趣时，用于获取当前天气信息。

        Args:
            city (string): 地点，例如：杭州，或者浙江杭州
        """
        if not self._on("weather"):
            return
        async for result in self._weather_current(event, city):
            yield result

    @filter.llm_tool(name="get_forecast_weather")
    async def get_forecast_weather_tool(self, event: AstrMessageEvent, city: str) -> MessageEventResult:
        """当用户对某个城市未来天气预报感兴趣时，用于获取天气预报信息。

        Args:
            city (string): 地点，例如：杭州，或者浙江杭州
        """
        if not self._on("weather"):
            return
        async for result in self._weather_forecast(event, city):
            yield result

    # ------------------------------------------------------------ 百度翻译

    @filter.command("fy")
    async def fy(self, event: AstrMessageEvent):
        """翻译：/fy <目标语种> <内容>，/fy help 查看语种"""
        if not self._on("translation"):
            return
        parts = (event.message_str or "").strip().split(" ", 2)
        if len(parts) < 2:
            yield event.plain_result(translation.USAGE)
            return
        if parts[1] == "help":
            yield event.plain_result(translation.help_text())
            return
        if len(parts) < 3 or not parts[2].strip():
            yield event.plain_result(translation.USAGE)
            return
        cfg = section(self.config, "translation")
        try:
            text = await translation.translate(
                str(cfg.get("appid") or ""), str(cfg.get("secret_key") or ""), parts[1], parts[2].strip()
            )
        except Exception as exc:
            text = f"调用翻译 API 时出错: {exc}"
        yield event.plain_result(text)

    # ------------------------------------------------------------ IP / Whois

    @filter.command("ip")
    async def ip_query(self, event: AstrMessageEvent):
        """查询 IP / 域名的归属地"""
        if not self._on("ipquery"):
            return
        target = ipquery.first_arg(event.message_str or "", "ip")
        yield event.plain_result(await ipquery.fetch_ip_info(target) if target else ipquery.IP_USAGE)

    @filter.command("whois")
    async def whois_query(self, event: AstrMessageEvent):
        """查询域名 Whois"""
        if not self._on("ipquery"):
            return
        target = ipquery.first_arg(event.message_str or "", "whois")
        yield event.plain_result(await ipquery.fetch_whois_info(target) if target else ipquery.WHOIS_USAGE)

    # ------------------------------------------------------------ AICU B 站评论查询

    @filter.command("uid")
    async def aicu_uid(self, event: AstrMessageEvent):
        """查询 B 站用户评论画像：/uid <UID>"""
        if not self._on("aicu"):
            return
        uid = command_rest(event).split(maxsplit=1)[0] if command_rest(event) else ""
        if not uid.isdigit():
            yield event.plain_result("❌ 请输入纯数字 UID，例如 /uid 2")
            return
        yield event.plain_result(f"🔍 正在获取 UID: {uid} 的数据...")
        try:
            path = await aicu.analyze(
                self.browser, uid, str(setting(self.config, "aicu", "cookie", "") or ""), self.data_dir / "tmp"
            )
        except Exception as exc:
            logger.error(f"[AICU] 处理失败: {exc}", exc_info=True)
            yield event.plain_result("❌ 插件运行错误，请查看后台日志。")
            return
        if path is None:
            yield event.plain_result("❌ 数据获取失败。请检查配置中的 Cookie 是否正确。")
            return
        yield event.image_result(path)

    # ------------------------------------------------------------ 数学绘图

    def _math_core(self):
        if self._math is None:
            from .omni.math_plotter import MathPlotterCore

            plots_dir = str(self.data_dir / "plots")
            self._math = MathPlotterCore(lambda key: section(self.config, "math_plotter").get(key), plots_dir)
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

    async def _math_call(self, name: str, event: AstrMessageEvent, **kwargs) -> str:
        if not self._on("math_plotter"):
            return "数学绘图功能已关闭"
        self._cleanup_plots()
        return await getattr(self._math_core(), name)(event, **kwargs)

    async def _math_command(self, event: AstrMessageEvent, name: str, **kwargs):
        text = await self._math_call(name, event, **kwargs)
        if isinstance(text, str) and text.startswith("❌"):
            yield event.plain_result(text)

    @filter.llm_tool(name="plot_function")
    async def tool_plot_function(
        self, event: AstrMessageEvent, expression: str, x_range: str = '', title: str = '', xlabel: str = '', ylabel: str = ''
    ) -> str:
        """绘制一元函数 y=f(x) 的图像。支持 sin、cos、tan、exp、log、sqrt、abs、pi 等。

        Args:
            expression(string): 函数表达式。例如 sin(x)、x**2、exp(-x**2)
            x_range(string): x 轴范围 "min,max"，默认 "-10,10"
            title(string): 图表标题，留空自动生成
            xlabel(string): x 轴标签，留空默认 "x"
            ylabel(string): y 轴标签，留空默认 "y"
        """
        return await self._math_call("tool_plot_function", event, expression=expression, x_range=x_range, title=title, xlabel=xlabel, ylabel=ylabel)

    @filter.llm_tool(name="plot_multiple")
    async def tool_plot_multiple(
        self, event: AstrMessageEvent, expressions: str, x_range: str = '', title: str = '函数对比', xlabel: str = '', ylabel: str = ''
    ) -> str:
        """在同一坐标系中绘制多个函数图像进行对比。

        Args:
            expressions(string): 逗号分隔的表达式。例如 "sin(x),cos(x),x**2"
            x_range(string): x 轴范围 "min,max"，默认 "-10,10"
            title(string): 图表标题
            xlabel(string): x 轴标签，留空默认 "x"
            ylabel(string): y 轴标签，留空默认 "y"
        """
        return await self._math_call("tool_plot_multiple", event, expressions=expressions, x_range=x_range, title=title, xlabel=xlabel, ylabel=ylabel)

    @filter.llm_tool(name="plot_implicit")
    async def tool_plot_implicit(
        self, event: AstrMessageEvent, equation: str, x_range: str = '', y_range: str = '', title: str = '', xlabel: str = '', ylabel: str = ''
    ) -> str:
        """绘制隐式方程 F(x,y)=0 的图像。将方程移项为 0 的形式传入。
        例如 x²+y²=1 → "x**2+y**2-1"，xy=1 → "x*y-1"。

        Args:
            equation(string): 移项为 0 的方程表达式
            x_range(string): x 轴范围，默认 "-5,5"
            y_range(string): y 轴范围，默认同 x_range
            title(string): 图表标题
            xlabel(string): x 轴标签，留空默认 "x"
            ylabel(string): y 轴标签，留空默认 "y"
        """
        return await self._math_call("tool_plot_implicit", event, equation=equation, x_range=x_range, y_range=y_range, title=title, xlabel=xlabel, ylabel=ylabel)

    @filter.llm_tool(name="plot_3d_function")
    async def tool_plot_3d_function(
        self, event: AstrMessageEvent, expression: str, x_range: str = '', y_range: str = '', title: str = '', xlabel: str = '', ylabel: str = '', zlabel: str = ''
    ) -> str:
        """绘制三维函数 z=f(x,y) 的曲面图像。变量为 x 和 y。

        Args:
            expression(string): 二元函数表达式。如 sin(sqrt(x**2+y**2))、x**2+y**2
            x_range(string): x 轴范围，默认 "-5,5"
            y_range(string): y 轴范围，默认同 x_range
            title(string): 图表标题
            xlabel(string): x 轴标签，留空默认 "x"
            ylabel(string): y 轴标签，留空默认 "y"
            zlabel(string): z 轴标签，留空默认 "z"
        """
        return await self._math_call("tool_plot_3d_function", event, expression=expression, x_range=x_range, y_range=y_range, title=title, xlabel=xlabel, ylabel=ylabel, zlabel=zlabel)

    @filter.llm_tool(name="plot_polar")
    async def tool_plot_polar(
        self, event: AstrMessageEvent, expression: str, theta_range: str = '', title: str = '', xlabel: str = '', ylabel: str = ''
    ) -> str:
        """绘制极坐标方程 r=f(θ) 的图像。变量为 theta。

        Args:
            expression(string): 极坐标表达式。例如 sin(3*theta)、1+cos(theta)、theta
            theta_range(string): θ 范围 "min,max"，默认 "0,6.2832"
            title(string): 图表标题
            xlabel(string): x 轴标签，留空默认 "x"
            ylabel(string): y 轴标签，留空默认 "y"
        """
        return await self._math_call("tool_plot_polar", event, expression=expression, theta_range=theta_range, title=title, xlabel=xlabel, ylabel=ylabel)

    @filter.llm_tool(name="plot_parametric")
    async def tool_plot_parametric(
        self, event: AstrMessageEvent, x_expression: str, y_expression: str, t_range: str = '', title: str = '', xlabel: str = '', ylabel: str = ''
    ) -> str:
        """绘制参数方程 x=f(t), y=g(t) 的图像。变量为 t。

        Args:
            x_expression(string): x(t) 表达式。例如 cos(t)、t-sin(t)
            y_expression(string): y(t) 表达式。例如 sin(t)、1-cos(t)
            t_range(string): t 范围 "min,max"，默认 "0,6.2832"
            title(string): 图表标题
            xlabel(string): x 轴标签，留空默认 "x"
            ylabel(string): y 轴标签，留空默认 "y"
        """
        return await self._math_call("tool_plot_parametric", event, x_expression=x_expression, y_expression=y_expression, t_range=t_range, title=title, xlabel=xlabel, ylabel=ylabel)

    @filter.llm_tool(name="plot_3d_spherical")
    async def tool_plot_3d_spherical(
        self, event: AstrMessageEvent, expression: str, theta_range: str = '', phi_range: str = '', title: str = '', xlabel: str = '', ylabel: str = '', zlabel: str = ''
    ) -> str:
        """绘制球坐标参数曲面 r=f(θ,φ)，变量 theta（极角）和 phi（方位角）。
        自动转换为直角坐标并用 plotly 渲染真 3D。
        例如朗伯体辐射分布: cos(theta) 会渲染为一个半球/甜甜圈形发光体。

        Args:
            expression(string): r(θ,φ) 表达式。例如 cos(theta)、1、sin(3*theta)*cos(2*phi)
            theta_range(string): θ 范围 "min,max"，默认 "0,3.1416"
            phi_range(string): φ 范围 "min,max"，默认 "0,6.2832"
            title(string): 图表标题
            xlabel(string): x 轴标签，留空默认 "x"
            ylabel(string): y 轴标签，留空默认 "y"
            zlabel(string): z 轴标签，留空默认 "z"
        """
        return await self._math_call("tool_plot_3d_spherical", event, expression=expression, theta_range=theta_range, phi_range=phi_range, title=title, xlabel=xlabel, ylabel=ylabel, zlabel=zlabel)

    @filter.llm_tool(name="plot_vector_field_2d")
    async def tool_plot_vector_field_2d(
        self, event: AstrMessageEvent, x_expression: str, y_expression: str, x_range: str = '', y_range: str = '', title: str = '', xlabel: str = '', ylabel: str = ''
    ) -> str:
        """绘制二维矢量场 (Fx(x,y), Fy(x,y)) 的箭头图。用于展示电场方向、流速场、梯度场等。
        箭头长度和方向表示矢量大小和方向。

        Args:
            x_expression(string): Fx(x,y) 表达式。例如 -x/sqrt(x**2+y**2)**3
            y_expression(string): Fy(x,y) 表达式。例如 -y/sqrt(x**2+y**2)**3
            x_range(string): x 范围 "min,max"，默认 "-5,5"
            y_range(string): y 范围，默认同 x_range
            title(string): 图表标题
            xlabel(string): x 轴标签，留空默认 "x"
            ylabel(string): y 轴标签，留空默认 "y"
        """
        return await self._math_call("tool_plot_vector_field_2d", event, x_expression=x_expression, y_expression=y_expression, x_range=x_range, y_range=y_range, title=title, xlabel=xlabel, ylabel=ylabel)

    @filter.llm_tool(name="plot_3d_multiple")
    async def tool_plot_3d_multiple(
        self, event: AstrMessageEvent, expressions: str, x_range: str = '', y_range: str = '', title: str = '3D 曲面对比', xlabel: str = '', ylabel: str = '', zlabel: str = ''
    ) -> str:
        """在同一 3D 坐标系中叠加多个曲面 z=f(x,y)，用于比较不同函数的空间形态。
        每个表达式用不同颜色区分，支持半透明叠加。

        Args:
            expressions(string): 逗号分隔的二元函数表达式。例如 "x**2+y**2, sqrt(x**2+y**2), sin(x)*cos(y)"
            x_range(string): x 范围 "min,max"，默认 "-5,5"
            y_range(string): y 范围，默认同 x_range
            title(string): 图表标题
            xlabel(string): x 轴标签，留空默认 "x"
            ylabel(string): y 轴标签，留空默认 "y"
            zlabel(string): z 轴标签，留空默认 "z"
        """
        return await self._math_call("tool_plot_3d_multiple", event, expressions=expressions, x_range=x_range, y_range=y_range, title=title, xlabel=xlabel, ylabel=ylabel, zlabel=zlabel)

    @filter.llm_tool(name="plot_implicit_3d")
    async def tool_plot_implicit_3d(
        self, event: AstrMessageEvent, equation: str, x_range: str = '', y_range: str = '', z_range: str = '', title: str = '', xlabel: str = '', ylabel: str = '', zlabel: str = ''
    ) -> str:
        """绘制隐式三维曲面 F(x,y,z)=0，如球面、双曲面、晶体结构等。
        将方程移项为 0 的形式传入。使用 plotly Isosurface 等值面渲染。

        Args:
            equation(string): 移项为 0 的方程。例如球面: "x**2+y**2+z**2-1"，双曲面: "x**2+y**2-z**2-1"
            x_range(string): x 范围 "min,max"，默认 "-3,3"
            y_range(string): y 范围，默认同 x_range
            z_range(string): z 范围，默认同 x_range
            title(string): 图表标题
            xlabel(string): x 轴标签，留空默认 "x"
            ylabel(string): y 轴标签，留空默认 "y"
            zlabel(string): z 轴标签，留空默认 "z"
        """
        return await self._math_call("tool_plot_implicit_3d", event, equation=equation, x_range=x_range, y_range=y_range, z_range=z_range, title=title, xlabel=xlabel, ylabel=ylabel, zlabel=zlabel)

    @filter.llm_tool(name="plot_3d_parametric")
    async def tool_plot_3d_parametric(
        self, event: AstrMessageEvent, x_expression: str, y_expression: str, z_expression: str, t_range: str = '', title: str = '', xlabel: str = '', ylabel: str = '', zlabel: str = ''
    ) -> str:
        """绘制三维参数曲线 (x(t), y(t), z(t))。用于展示空间螺线、轨迹、光线路径等。
        变量为 t。使用 matplotlib 3D 渲染。

        Args:
            x_expression(string): x(t) 表达式。例如 cos(t)、sin(2*t)
            y_expression(string): y(t) 表达式。例如 sin(t)、cos(3*t)
            z_expression(string): z(t) 表达式。例如 t、t/5
            t_range(string): t 范围 "min,max"，默认 "0,12.5664"（0 到 4π）
            title(string): 图表标题
            xlabel(string): x 轴标签，留空默认 "x"
            ylabel(string): y 轴标签，留空默认 "y"
            zlabel(string): z 轴标签，留空默认 "z"
        """
        return await self._math_call("tool_plot_3d_parametric", event, x_expression=x_expression, y_expression=y_expression, z_expression=z_expression, t_range=t_range, title=title, xlabel=xlabel, ylabel=ylabel, zlabel=zlabel)

    @filter.llm_tool(name="plot_vector_2d")
    async def tool_plot_vector_2d(
        self, event: AstrMessageEvent, vectors: str, title: str = '', xlabel: str = '', ylabel: str = ''
    ) -> str:
        """绘制二维向量示意图。每个向量用带实心三角箭头的线段表示（箭头在终点）。

        Args:
            vectors(string): 向量定义，多个向量用英文分号 ";" 分隔。每个向量格式：
                "x,y :颜色:标签" → 从原点出发
                "(x1,y1)->(x2,y2) :颜色:标签" → 指定起终点
                例："3,4:red:v1 ; 0,0->2,3:blue:v2"
            title(string): 图表标题，留空自动生成
            xlabel(string): x 轴标签，留空默认 "x"
            ylabel(string): y 轴标签，留空默认 "y"
        """
        return await self._math_call("tool_plot_vector_2d", event, vectors=vectors, title=title, xlabel=xlabel, ylabel=ylabel)

    @filter.llm_tool(name="plot_vector_3d")
    async def tool_plot_vector_3d(
        self, event: AstrMessageEvent, vectors: str, title: str = '', xlabel: str = '', ylabel: str = '', zlabel: str = ''
    ) -> str:
        """绘制三维向量示意图。每个向量用线段 + 末端锥体箭头表示。

        Args:
            vectors(string): 向量定义，多个向量用英文分号 ";" 分隔。格式：
                "x,y,z :颜色:标签" → 从原点出发
                "(x1,y1,z1)->(x2,y2,z2) :颜色:标签" → 指定起终点
                例："1,2,3:red:v1 ; 0,0,0->3,4,1:blue:v2"
            title(string): 图表标题
            xlabel(string): x 轴标签，留空默认 "x"
            ylabel(string): y 轴标签，留空默认 "y"
            zlabel(string): z 轴标签，留空默认 "z"
        """
        return await self._math_call("tool_plot_vector_3d", event, vectors=vectors, title=title, xlabel=xlabel, ylabel=ylabel, zlabel=zlabel)

    @filter.llm_tool(name="plot_vector_addition")
    async def tool_plot_vector_addition(
        self, event: AstrMessageEvent, v1: str, v2: str, title: str = '', xlabel: str = '', ylabel: str = ''
    ) -> str:
        """演示向量加法 v1 + v2，用平行四边形法则显示结果向量。

        Args:
            v1(string): 第一个向量分量，格式 "x,y"。例如 "1,2"
            v2(string): 第二个向量分量，格式 "x,y"。例如 "3,1"
            title(string): 图表标题，留空自动生成
            xlabel(string): x 轴标签
            ylabel(string): y 轴标签
        """
        return await self._math_call("tool_plot_vector_addition", event, v1=v1, v2=v2, title=title, xlabel=xlabel, ylabel=ylabel)

    @filter.llm_tool(name="plot_vector_scaling")
    async def tool_plot_vector_scaling(
        self, event: AstrMessageEvent, vector: str, scalar: float = 2.0, title: str = '', xlabel: str = '', ylabel: str = ''
    ) -> str:
        """演示向量数乘 k·v，同时显示原向量和缩放后的向量（颜色区分）。

        Args:
            vector(string): 向量分量，格式 "x,y"。例如 "1,3"
            scalar(number): 标量 k，默认 2.0。支持负数（反向）、小数（缩短）
            title(string): 图表标题
            xlabel(string): x 轴标签
            ylabel(string): y 轴标签
        """
        return await self._math_call("tool_plot_vector_scaling", event, vector=vector, scalar=scalar, title=title, xlabel=xlabel, ylabel=ylabel)

    @filter.llm_tool(name="plot_basis_transform")
    async def tool_plot_basis_transform(
        self, event: AstrMessageEvent, new_basis: str, title: str = '', xlabel: str = '', ylabel: str = ''
    ) -> str:
        """演示基变换：在同一坐标系中显示标准基向量 e1, e2 和一组新基向量。

        Args:
            new_basis(string): 新基向量，格式 "(b1x,b1y),(b2x,b2y)"。
                例："(1,1),(1,-1)" 表示新基 b1=(1,1), b2=(1,-1)
            title(string): 图表标题
            xlabel(string): x 轴标签
            ylabel(string): y 轴标签
        """
        return await self._math_call("tool_plot_basis_transform", event, new_basis=new_basis, title=title, xlabel=xlabel, ylabel=ylabel)

    @filter.command("vector", desc="绘制二维向量示意图（支持多向量、自定义颜色和标签）")
    async def cmd_vector(self, event: AstrMessageEvent):
        if not self._on("math_plotter"):
            return
        expr = command_rest(event)
        if not expr:
            yield event.plain_result(
                "用法：/vector <向量定义>[; <向量定义> ...]\n"
                "格式：\"x,y:颜色:标签\" 或 \"(x1,y1)->(x2,y2):颜色:标签\"\n"
                "例如：/vector 3,4:red:v1 ; 0,0->2,3:blue:v2")
            return
        async for r in self._math_command(event, "tool_plot_vector_2d", vectors=expr):
            yield r

    @filter.command("vector3d", desc="绘制三维向量示意图（线段+末端锥体箭头）")
    async def cmd_vector3d(self, event: AstrMessageEvent):
        if not self._on("math_plotter"):
            return
        expr = command_rest(event)
        if not expr:
            yield event.plain_result(
                "用法：/vector3d <向量定义>[; <向量定义> ...]\n"
                "格式：\"x,y,z:颜色:标签\" 或 \"(x1,y1,z1)->(x2,y2,z2):颜色:标签\"\n"
                "例如：/vector3d 1,2,3:red ; 0,0,0->3,4,1:blue")
            return
        async for r in self._math_command(event, "tool_plot_vector_3d", vectors=expr):
            yield r

    @filter.command("vector_add", desc="演示向量加法（平行四边形法则，显示结果向量）")
    async def cmd_vector_add(self, event: AstrMessageEvent):
        if not self._on("math_plotter"):
            return
        expr = command_rest(event)
        parts = expr.split("+")
        if not expr or len(parts) != 2:
            yield event.plain_result("用法：/vector_add <v1> + <v2>\n例如：/vector_add 1,2 + 3,1")
            return
        async for r in self._math_command(event, "tool_plot_vector_addition", v1=parts[0].strip(), v2=parts[1].strip()):
            yield r

    @filter.command("vector_scale", desc="演示向量数乘 k·v（颜色区分原向量和缩放向量）")
    async def cmd_vector_scale(self, event: AstrMessageEvent):
        if not self._on("math_plotter"):
            return
        expr = command_rest(event)
        if not expr or "*" not in expr:
            yield event.plain_result(
                "用法：/vector_scale <k> * <向量>\n例如：/vector_scale 2 * 1,3\n     /vector_scale -0.5 * 4,2")
            return
        k_text, vector = expr.split("*", 1)
        try:
            k = float(k_text.strip())
        except ValueError:
            yield event.plain_result("❌ 标量 k 必须是数字。")
            return
        async for r in self._math_command(event, "tool_plot_vector_scaling", vector=vector.strip(), scalar=k):
            yield r

    @filter.command("basis", desc="演示基变换（标准基 vs 新基在同一坐标系中）")
    async def cmd_basis(self, event: AstrMessageEvent):
        if not self._on("math_plotter"):
            return
        expr = command_rest(event)
        if not expr:
            yield event.plain_result(
                "用法：/basis <(b1x,b1y),(b2x,b2y)>\n例如：/basis (1,1),(1,-1)\n     /basis (2,0),(0,3)")
            return
        async for r in self._math_command(event, "tool_plot_basis_transform", new_basis=expr):
            yield r

    @filter.command("plot", desc="绘制一元函数图像 y=f(x)，支持隐式方程 F(x,y)=0 和多函数对比")
    async def cmd_plot(self, event: AstrMessageEvent):
        if not self._on("math_plotter"):
            return
        expr = command_rest(event)
        if not expr:
            yield event.plain_result("用法：/plot <表达式>\n例如：/plot sin(x)")
            return
        if "," in expr:
            gen = self._math_command(event, "tool_plot_multiple", expressions=expr)
        else:
            gen = self._math_command(event, "tool_plot_function", expression=expr)
        async for r in gen:
            yield r

    @filter.command("plot_status", desc="查看数学绘图插件状态（缓存图像数、占用空间、DPI等）")
    async def cmd_plot_status(self, event: AstrMessageEvent):
        if not self._on("math_plotter"):
            return
        plots = self.data_dir / "plots"
        files = list(plots.glob("*.png")) if plots.exists() else []
        size = sum(f.stat().st_size for f in files)
        cfg = section(self.config, "math_plotter")
        yield event.plain_result(
            f"📊 数学绘图插件状态\n📁 缓存图像：{len(files)} 个\n"
            f"💾 占用空间：{size / 1024:.1f} KB\n"
            f"📐 DPI：{cfg.get('plot_dpi', 120)}\n"
            f"📏 默认范围：{cfg.get('default_x_range', '-10,10')}"
        )

    @filter.command("plot3d", desc="绘制三维曲面 z=f(x,y)")
    async def cmd_plot3d(self, event: AstrMessageEvent):
        if not self._on("math_plotter"):
            return
        expr = command_rest(event)
        if not expr:
            yield event.plain_result("用法：/plot3d <二元函数>\n例如：/plot3d x**2+y**2")
            return
        async for r in self._math_command(event, "tool_plot_3d_function", expression=expr):
            yield r

    @filter.command("polar", desc="绘制极坐标图像 r=f(θ)")
    async def cmd_polar(self, event: AstrMessageEvent):
        if not self._on("math_plotter"):
            return
        expr = command_rest(event)
        if not expr:
            yield event.plain_result("用法：/polar <表达式>\n例如：/polar sin(3*theta)")
            return
        async for r in self._math_command(event, "tool_plot_polar", expression=expr):
            yield r

    @filter.command("parametric", desc="绘制二维参数方程图像 (x(t), y(t))")
    async def cmd_parametric(self, event: AstrMessageEvent):
        if not self._on("math_plotter"):
            return
        expr = command_rest(event)
        if not expr or "," not in expr:
            yield event.plain_result("用法：/parametric <x>,<y>\n例如：/parametric cos(t),sin(t)")
            return
        x_expr, y_expr = expr.split(",", 1)
        async for r in self._math_command(
            event, "tool_plot_parametric", x_expression=x_expr.strip(), y_expression=y_expr.strip()
        ):
            yield r

    @filter.command("spherical", desc="绘制球坐标曲面 r=f(θ,φ)")
    async def cmd_spherical(self, event: AstrMessageEvent):
        if not self._on("math_plotter"):
            return
        expr = command_rest(event)
        if not expr:
            yield event.plain_result(
                "用法：/spherical <球坐标表达式 r(θ,φ)>\n例如：/spherical cos(theta)\n     /spherical 1")
            return
        async for r in self._math_command(event, "tool_plot_3d_spherical", expression=expr):
            yield r

    @filter.command("vector2d", desc="绘制二维矢量场 F=(Fx, Fy)")
    async def cmd_vector2d(self, event: AstrMessageEvent):
        if not self._on("math_plotter"):
            return
        expr = command_rest(event)
        if not expr or "," not in expr:
            yield event.plain_result("用法：/vector2d <Fx表达式>,<Fy表达式>\n例如：/vector2d -x,-y")
            return
        fx, fy = expr.split(",", 1)
        async for r in self._math_command(
            event, "tool_plot_vector_field_2d", x_expression=fx.strip(), y_expression=fy.strip()
        ):
            yield r

    @filter.command("plot3dm", desc="绘制多个三维曲面叠加对比")
    async def cmd_plot3dm(self, event: AstrMessageEvent):
        if not self._on("math_plotter"):
            return
        expr = command_rest(event)
        if not expr:
            yield event.plain_result(
                "用法：/plot3dm <表达式1>,<表达式2>,...\n例如：/plot3dm x**2+y**2, sqrt(x**2+y**2)")
            return
        async for r in self._math_command(event, "tool_plot_3d_multiple", expressions=expr):
            yield r

    @filter.command("implicit3d", desc="绘制隐式三维曲面 F(x,y,z)=0")
    async def cmd_implicit3d(self, event: AstrMessageEvent):
        if not self._on("math_plotter"):
            return
        expr = command_rest(event)
        if not expr:
            yield event.plain_result(
                "用法：/implicit3d <隐式方程=0>\n例如：/implicit3d x**2+y**2+z**2-1\n     /implicit3d x**2+y**2-z**2-1")
            return
        async for r in self._math_command(event, "tool_plot_implicit_3d", equation=expr):
            yield r

    @filter.command("parametric3d", desc="绘制三维参数曲线 (x(t), y(t), z(t))")
    async def cmd_parametric3d(self, event: AstrMessageEvent):
        if not self._on("math_plotter"):
            return
        expr = command_rest(event)
        parts = expr.split(",")
        if not expr or len(parts) < 3:
            yield event.plain_result(
                "用法：/parametric3d <x(t)>,<y(t)>,<z(t)>\n"
                "例如：/parametric3d cos(t),sin(t),t/5\n"
                "     /parametric3d sin(t),cos(t),sin(2*t)")
            return
        async for r in self._math_command(
            event, "tool_plot_3d_parametric",
            x_expression=parts[0].strip(), y_expression=parts[1].strip(), z_expression=parts[2].strip(),
        ):
            yield r

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
        """取消订阅点赞"""
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

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.command("订阅点赞列表")
    async def like_list(self, event: AstrMessageEvent):
        """查看订阅点赞的用户"""
        if not self._on("zanwo"):
            return
        users = [str(u) for u in self._zanwo_cfg().get("subscribed_users") or []]
        if not users:
            yield event.plain_result("当前没有订阅点赞的用户哦~")
            return
        yield event.plain_result("当前订阅点赞的用户ID列表：\n" + "\n".join(users))

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("谁赞了bot", alias={"谁赞了你"})
    async def get_profile_like(self, event: AstrMessageEvent):
        """获取 bot 自身的点赞列表（管理员）"""
        if not self._on("zanwo"):
            return
        data = await event.bot.get_profile_like()
        url = await self.text_to_image(zanwo.profile_like_text(data))
        yield event.image_result(url)
