import copy

import inspect

import json

import mimetypes

import re

from pathlib import Path

from typing import Any

from mcp.types import CallToolResult, ImageContent, TextContent

from astrbot.api import AstrBotConfig, logger

from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter

from astrbot.api.star import Context, Star

from astrbot.core.message.components import At, Image, Node, Nodes, Plain, Reply

from astrbot.core.pipeline.context_utils import call_handler

from astrbot.core.star.filter.command import CommandFilter

from astrbot.core.star.filter.command_group import CommandGroupFilter

from astrbot.core.star.filter.event_message_type import EventMessageTypeFilter

from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter

from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterTypeFilter

from astrbot.core.star.star_handler import StarHandlerMetadata, star_handlers_registry

QQ_OFFICIAL_PLATFORMS = frozenset({'qq_official', 'qq_official_webhook'})

QQ_OFFICIAL_TEXT_LIMIT = 3900

class BotIdentityEventWrapper:
    """
    事件包装器，用于覆盖 get_sender_id() / get_sender_name() 方法返回Bot的身份
    """

    def __init__(self, original_event: AstrMessageEvent, bot_user_id: str, bot_nickname: str='Bot'):
        """Create an isolated logical Bot identity for command handlers.

        Args:
            original_event: Routable platform event that remains unchanged.
            bot_user_id: Synthetic sender ID exposed to the command handler.
            bot_nickname: Synthetic sender name exposed to the command handler.
        """
        self._original_event = original_event
        self._bot_user_id = bot_user_id
        self._bot_nickname = bot_nickname
        self._message_obj_override = None
        message_obj = getattr(original_event, 'message_obj', None)
        if message_obj is not None:
            try:
                message_obj_override = copy.copy(message_obj)
                sender = getattr(message_obj, 'sender', None)
                if sender is not None:
                    sender_override = copy.copy(sender)
                    if hasattr(sender_override, 'user_id'):
                        sender_override.user_id = bot_user_id
                    if hasattr(sender_override, 'nickname'):
                        sender_override.nickname = bot_nickname
                    message_obj_override.sender = sender_override
                raw_message = getattr(message_obj, 'raw_message', None)
                if isinstance(raw_message, dict):
                    raw_override = copy.deepcopy(raw_message)
                    raw_override['user_id'] = bot_user_id
                    if isinstance(raw_override.get('sender'), dict):
                        raw_override['sender']['user_id'] = bot_user_id
                        raw_override['sender']['nickname'] = bot_nickname
                    message_obj_override.raw_message = raw_override
                elif raw_message is not None:
                    raw_override = copy.copy(raw_message)
                    author = getattr(raw_message, 'author', None)
                    if author is not None:
                        author_override = copy.copy(author)
                        for attr in ('id', 'user_id', 'member_openid', 'user_openid'):
                            if hasattr(author_override, attr):
                                try:
                                    setattr(author_override, attr, bot_user_id)
                                except Exception:
                                    pass
                        for attr in ('username', 'nickname'):
                            if hasattr(author_override, attr):
                                try:
                                    setattr(author_override, attr, bot_nickname)
                                except Exception:
                                    pass
                        try:
                            raw_override.author = author_override
                        except Exception:
                            pass
                    message_obj_override.raw_message = raw_override
                self._message_obj_override = message_obj_override
            except Exception as e:
                logger.debug(f'构建Bot身份消息副本失败，回退到事件方法包装: {e}')

    def get_sender_id(self):
        """返回Bot的ID而不是原始发送者ID"""
        return self._bot_user_id

    def get_sender_name(self):
        """返回Bot的昵称而不是原始发送者昵称"""
        return self._bot_nickname

    def __getattr__(self, name):
        """其他所有属性和方法都委托给原始事件对象"""
        if name == 'message_obj' and self._message_obj_override is not None:
            return self._message_obj_override
        return getattr(self._original_event, name)

    def __setattr__(self, name, value):
        """设置属性"""
        if name in ('_original_event', '_bot_user_id', '_bot_nickname', '_message_obj_override'):
            object.__setattr__(self, name, value)
        elif name == 'message_obj' and self._message_obj_override is not None:
            object.__setattr__(self, '_message_obj_override', value)
        else:
            setattr(self._original_event, name, value)

class LLMExecutorCore:
    """
    AstrBot LLM 指令执行器插件 v1.3

    【核心功能】
    让 LLM 能够代理执行普通插件指令，或以 Bot 自身执行指令，实现自然语言到指令的转换。

    【设计理念】
    - 配合 astrbot_plugin_command_query 使用
    - command_query 负责：查询指令名（LLM 用 search_command 工具查找指令）
    - llm_executor 负责：执行指令（LLM 用 execute_command 工具执行指令）

    【工作流程示例】
    1. 用户说："帮我钓鱼"
    2. LLM 可能先调用 search_command(keyword="钓鱼") 确认指令存在
    3. LLM 调用 execute_command(command="钓鱼") 执行指令
    4. 插件执行指令并返回结果
    5. LLM 组织自然语言回复用户
    """

    def __init__(self, context: Context, config: AstrBotConfig=None):
        """插件初始化"""
        self.context = context
        self.config = config or {}
        self._handler_cache: dict[str, dict] = {}
        self._alias_to_command: dict[str, str] = {}
        self.enabled = self.config.get('enabled', True)
        self.whitelist: list[str] = self.config.get('whitelist', [])
        self.blacklist: list[str] = self.config.get('blacklist', [])
        self.allow_admin_commands = self.config.get('allow_admin_commands', False)
        self.admin_users: list[str] = self.config.get('admin_users', [])
        self.bot_user_id = self.config.get('bot_user_id', 'bot_self')
        bot_user_id_by_platform = self.config.get('bot_user_id_by_platform', self.config.get('bot_user_id_map', {}))
        self.bot_user_id_by_platform: dict[str, str] = bot_user_id_by_platform if isinstance(bot_user_id_by_platform, dict) else {}
        self.bot_nickname = self.config.get('bot_nickname', 'Bot')
        self.enable_forward = self.config.get('enable_forward', True)
        self.forward_threshold = self.config.get('forward_threshold', 1500)
        self.at_position_mode = self.config.get('at_position_mode', 'before_args')
        self.command_at_position_map: dict[str, str] = self.config.get('command_at_position_map', {})
        logger.info('LLM指令执行器插件已加载 v1.3.0')
        logger.info(f'  - 启用状态: {self.enabled}')
        logger.info(f'  - 白名单: {(self.whitelist if self.whitelist else '无限制')}')
        logger.info(f'  - 黑名单: {(self.blacklist if self.blacklist else '无')}')
        logger.info(f'  - 允许管理员指令: {self.allow_admin_commands}')
        logger.info(f'  - 管理员用户: {(self.admin_users if self.admin_users else '无')}')
        logger.info(f'  - Bot用户ID: {self.bot_user_id}')
        logger.info(f'  - 平台Bot用户ID: {(self.bot_user_id_by_platform if self.bot_user_id_by_platform else '未配置')}')
        logger.info(f'  - Bot昵称: {self.bot_nickname}')
        logger.info(f'  - 合并转发: {('启用' if self.enable_forward else '禁用')} (阈值: {self.forward_threshold}字)')

    async def _initialize(self):
        """异步初始化，构建指令处理器缓存"""
        self._build_handler_cache()
        logger.info(f'已缓存 {len(self._handler_cache)} 个指令处理器')

    def _build_handler_cache(self):
        """构建指令名到处理器的映射 - 优化版 O(N+M)"""
        self._handler_cache.clear()
        self._alias_to_command.clear()
        try:
            all_stars = self.context.get_all_stars()
            all_stars = [star for star in all_stars if star.activated]
        except Exception as e:
            logger.error(f'获取插件列表失败: {e}')
            return
        if not all_stars:
            logger.warning('没有找到任何激活的插件')
            return
        skip_plugins = {'astrbot', 'astrbot_plugin_llm_executor', 'astrbot_plugin_command_query', 'astrbot-reminder'}
        module_to_star = {}
        for star in all_stars:
            plugin_name = getattr(star, 'name', '未知插件')
            module_path = getattr(star, 'module_path', None)
            if plugin_name in skip_plugins or not module_path:
                continue
            module_to_star[module_path] = (star, plugin_name)
        for handler in star_handlers_registry:
            if not isinstance(handler, StarHandlerMetadata):
                continue
            if not handler.enabled:
                continue
            star_info = module_to_star.get(handler.handler_module_path)
            if not star_info:
                continue
            star, plugin_name = star_info
            command_name = None
            aliases = []
            description = handler.desc or '无描述'
            is_admin_command = False
            for filter_ in handler.event_filters:
                if isinstance(filter_, CommandFilter):
                    complete_names = filter_.get_complete_command_names()
                    if complete_names:
                        command_name = complete_names[0]
                        aliases = complete_names[1:]
                    else:
                        command_name = filter_.command_name
                        aliases = list(filter_.alias) if getattr(filter_, 'alias', None) else []
                elif isinstance(filter_, CommandGroupFilter):
                    command_name = filter_.group_name
                    aliases = list(filter_.alias) if getattr(filter_, 'alias', None) else []
                elif isinstance(filter_, PermissionTypeFilter):
                    is_admin_command = filter_.permission_type == PermissionType.ADMIN
            if command_name:
                command_name = command_name.lstrip('/')
                aliases = [alias.lstrip('/') for alias in aliases]
                handler_info = {'command': command_name, 'description': description, 'plugin': plugin_name, 'aliases': aliases, 'is_admin': is_admin_command, 'handler': handler, 'module_path': handler.handler_module_path, 'reserved': bool(getattr(star, 'reserved', False)), 'support_platforms': list(getattr(star, 'support_platforms', []) or [])}
                self._handler_cache[command_name] = handler_info
                for alias in aliases:
                    self._alias_to_command[alias] = command_name

    def _can_execute(self, command: str, event: AstrMessageEvent, as_bot: bool=False) -> tuple[bool, str]:
        """
        检查是否可以执行指令

        Args:
            command: 指令名（不含前缀）
            event: 消息事件

        Returns:
            (是否可执行, 原因)
        """
        if not self.enabled:
            return (False, 'LLM指令执行器已禁用')
        command = command.lstrip('/~～')
        actual_command = self._alias_to_command.get(command, command)
        if actual_command not in self._handler_cache:
            return (False, f'未找到指令: {command}')
        handler_info = self._handler_cache[actual_command]
        enabled_plugins = getattr(event, 'plugins_name', None)
        if enabled_plugins is not None and (not handler_info.get('reserved', False)) and (handler_info['plugin'] not in enabled_plugins):
            return (False, f'指令 {command} 所属插件未在当前 Bot 配置中启用')
        platform_name = event.get_platform_name()
        support_platforms = handler_info.get('support_platforms', [])
        if support_platforms and platform_name not in support_platforms:
            return (False, f'指令 {command} 不兼容当前平台 {platform_name}')
        cfg = self._get_astrbot_config(event)
        for filter_ in handler_info['handler'].event_filters:
            if isinstance(filter_, (PlatformAdapterTypeFilter, EventMessageTypeFilter)):
                try:
                    filter_passed = filter_.filter(event, cfg)
                except Exception as e:
                    logger.debug(f'检查指令 {command} 平台兼容性失败: {e}')
                    return (False, f'无法确认指令 {command} 是否兼容当前平台')
                if not filter_passed:
                    return (False, f'指令 {command} 不适用于当前平台或会话类型')
        if self.whitelist:
            if actual_command not in self.whitelist and command not in self.whitelist:
                return (False, f'指令 {command} 不在白名单中')
        if self.blacklist:
            if actual_command in self.blacklist or command in self.blacklist:
                return (False, f'指令 {command} 在黑名单中')
        if handler_info.get('is_admin', False):
            user_id = str(event.get_sender_id())
            if callable(getattr(event, 'is_admin', None)) and event.is_admin():
                return (True, '可以执行（当前会话框架管理员）')
            if user_id in self.admin_users:
                return (True, '可以执行（管理员用户）')
            if as_bot and self._is_bot_framework_admin(event, self._resolve_bot_user_id(event)):
                return (True, '可以执行（Bot框架管理员身份）')
            if not self.allow_admin_commands:
                return (False, f'指令 {command} 需要管理员权限，你不在管理员列表中')
        return (True, '可以执行')

    def _get_plugin_instance(self, module_path: str) -> Star | None:
        """
        获取处理器所属的插件实例

        Args:
            module_path: 处理器的模块路径

        Returns:
            插件实例或None
        """
        try:
            all_stars = self.context.get_all_stars()
            for star in all_stars:
                if getattr(star, 'module_path', None) == module_path:
                    return star.star_cls
            return None
        except Exception as e:
            logger.error(f'获取插件实例失败: {e}')
            return None

    def _resolve_at_position_mode(self, command: str) -> str:
        """解析指定指令的 @ 默认位置策略。"""
        command = (command or '').lstrip('/~～')
        mode = self.command_at_position_map.get(command, self.at_position_mode)
        return mode if mode in ('before_args', 'after_args') else 'before_args'

    def _get_astrbot_config(self, event: AstrMessageEvent | None=None):
        """Return the AstrBot config associated with the current session.

        Args:
            event: Message event used to resolve a profile-specific config.

        Returns:
            The matching AstrBot config, or the plugin config as a fallback.
        """
        get_config = getattr(self.context, 'get_config', None)
        if callable(get_config):
            try:
                umo = event.unified_msg_origin if event is not None else None
                return get_config(umo)
            except Exception as e:
                logger.debug(f'获取会话 AstrBot 配置失败，使用回退配置: {e}')
        return getattr(self.context, 'astrbot_config', None) or self.config

    def _resolve_bot_user_id(self, event: AstrMessageEvent) -> str:
        """Resolve the synthetic Bot identity for the event platform.

        Args:
            event: Current message event.

        Returns:
            Platform-specific Bot identity used by downstream plugins.
        """
        platform_name = event.get_platform_name()
        platform_user_id = str(self.bot_user_id_by_platform.get(platform_name, '') or '').strip()
        if platform_user_id:
            return platform_user_id
        bot_user_id = str(self.bot_user_id or '').strip()
        if bot_user_id and bot_user_id != 'bot_self':
            return bot_user_id
        if platform_name in QQ_OFFICIAL_PLATFORMS:
            platform_id = str(event.get_platform_id() or '').strip()
            if platform_id:
                return f'{platform_name}:{platform_id}'
        return bot_user_id or 'bot_self'

    def _unwrap_message_event(self, event_or_context: Any) -> AstrMessageEvent | None:
        """兼容新版工具链传入的 ContextWrapper，取出真实消息事件。"""
        candidates = [event_or_context]
        seen = set()
        while candidates:
            candidate = candidates.pop(0)
            if candidate is None:
                continue
            marker = id(candidate)
            if marker in seen:
                continue
            seen.add(marker)
            if callable(getattr(candidate, 'get_sender_id', None)) and callable(getattr(candidate, 'get_platform_name', None)):
                return candidate
            inner_context = getattr(candidate, 'context', None)
            if inner_context is not None:
                candidates.append(getattr(inner_context, 'event', None))
                candidates.append(inner_context)
            candidates.append(getattr(candidate, 'event', None))
        return None

    def _describe_event_like(self, event_or_context: Any) -> str:
        if event_or_context is None:
            return 'None'
        type_name = f'{type(event_or_context).__module__}.{type(event_or_context).__qualname__}'
        inner_context = getattr(event_or_context, 'context', None)
        if inner_context is None:
            return type_name
        return f'{type_name}(context={type(inner_context).__module__}.{type(inner_context).__qualname__})'

    def _is_configured_admin(self, event: AstrMessageEvent) -> bool:
        user_id = str(event.get_sender_id())
        is_framework_admin = callable(getattr(event, 'is_admin', None)) and event.is_admin()
        return is_framework_admin or user_id in self.admin_users or self.allow_admin_commands

    def _is_bot_framework_admin(self, event: AstrMessageEvent, bot_user_id: str | None=None) -> bool:
        """Check whether the synthetic Bot identity is a profile administrator.

        Args:
            event: Current message event used to resolve its profile config.
            bot_user_id: Already resolved Bot identity, if available.

        Returns:
            Whether the identity appears in the profile's administrator list.
        """
        cfg = self._get_astrbot_config(event)
        admins_id = cfg.get('admins_id', []) if hasattr(cfg, 'get') else []
        identity = bot_user_id or self._resolve_bot_user_id(event)
        return str(identity) in {str(admin_id) for admin_id in admins_id}

    def _snapshot_event_state(self, event: AstrMessageEvent) -> dict[str, Any]:
        message_obj = getattr(event, 'message_obj', None)
        sender = getattr(message_obj, 'sender', None) if message_obj else None
        raw_message = getattr(message_obj, 'raw_message', None) if message_obj else None
        return {'message_str': event.message_str, 'message_obj': message_obj, 'message_obj_message': copy.copy(getattr(message_obj, 'message', None)) if message_obj else None, 'message_obj_message_str': getattr(message_obj, 'message_str', None) if message_obj else None, 'sender': sender, 'sender_state': copy.copy(getattr(sender, '__dict__', {})) if sender else None, 'raw_message': raw_message, 'raw_message_state': copy.deepcopy(raw_message) if isinstance(raw_message, dict) else None, 'is_wake': getattr(event, 'is_wake', None), 'is_at_or_wake_command': getattr(event, 'is_at_or_wake_command', None), 'role': getattr(event, 'role', None)}

    def _restore_event_state(self, event: AstrMessageEvent, state: dict[str, Any]) -> None:
        event.message_str = state['message_str']
        event.message_obj = state['message_obj']
        message_obj = state['message_obj']
        if message_obj is not None:
            if state['message_obj_message'] is not None:
                message_obj.message = state['message_obj_message']
            if state['message_obj_message_str'] is not None:
                message_obj.message_str = state['message_obj_message_str']
        sender = state['sender']
        sender_state = state['sender_state']
        if sender is not None and sender_state is not None:
            sender.__dict__.clear()
            sender.__dict__.update(sender_state)
        raw_message = state['raw_message']
        raw_message_state = state['raw_message_state']
        if isinstance(raw_message, dict) and raw_message_state is not None:
            raw_message.clear()
            raw_message.update(raw_message_state)
        if state['is_wake'] is not None:
            event.is_wake = state['is_wake']
        if state['is_at_or_wake_command'] is not None:
            event.is_at_or_wake_command = state['is_at_or_wake_command']
        if state['role'] is not None:
            event.role = state['role']
        if hasattr(event, '_extras'):
            event._extras.pop('parsed_params', None)

    def _apply_bot_identity(self, event: AstrMessageEvent, bot_user_id: str) -> None:
        """Apply role-only identity state to the original routable event.

        Args:
            event: Original AstrBot message event.
            bot_user_id: Resolved synthetic Bot identity.
        """
        if self._is_bot_framework_admin(event, bot_user_id):
            event.role = 'admin'

    def _validate_handler_filters(self, handler: StarHandlerMetadata, event: AstrMessageEvent) -> tuple[bool, str, dict[str, Any]]:
        cfg = self._get_astrbot_config(event)
        original_message_str = event.message_str
        event.message_str = original_message_str.lstrip('/')
        if hasattr(event, 'message_obj') and hasattr(event.message_obj, 'message_str'):
            event.message_obj.message_str = event.message_str
        event.is_wake = True
        event.is_at_or_wake_command = True
        if hasattr(event, '_extras'):
            event._extras.pop('parsed_params', None)
        try:
            for filter_ in handler.event_filters:
                try:
                    if isinstance(filter_, PermissionTypeFilter):
                        if filter_.filter(event, cfg):
                            continue
                        if filter_.permission_type == PermissionType.ADMIN and self._is_configured_admin(event):
                            continue
                        return (False, f'指令 {handler.handler_name} 权限校验未通过', {})
                    if not filter_.filter(event, cfg):
                        if isinstance(filter_, PlatformAdapterTypeFilter):
                            return (False, f'当前平台 {event.get_platform_name()} 不支持指令 {handler.handler_name}', {})
                        if isinstance(filter_, EventMessageTypeFilter):
                            return (False, f'当前会话类型不支持指令 {handler.handler_name}', {})
                        return (False, f'指令 {handler.handler_name} 的过滤器校验未通过', {})
                except Exception as e:
                    return (False, f'指令参数或过滤器校验失败: {e}', {})
            params = event.get_extra('parsed_params', {}) if hasattr(event, 'get_extra') else {}
            return (True, '可以执行', params or {})
        finally:
            event.message_str = original_message_str
            if hasattr(event, 'message_obj') and hasattr(event.message_obj, 'message_str'):
                event.message_obj.message_str = original_message_str
            if hasattr(event, '_extras'):
                event._extras.pop('parsed_params', None)

    def _normalize_image_output(self, image_value: Any) -> str:
        """将图片输出归一化为可回传给上游的稳定地址格式"""
        if image_value is None:
            return ''
        image_str = str(image_value).strip()
        if not image_str:
            return ''
        lower_str = image_str.lower()
        if lower_str.startswith(('http://', 'https://', 'data:')):
            return image_str
        if lower_str.startswith('file://'):
            try:
                raw_path = image_str[7:]
                while raw_path.startswith('/') and len(raw_path) > 2 and (raw_path[2] == ':'):
                    raw_path = raw_path[1:]
                file_uri = Path(raw_path).resolve().as_uri()
                return file_uri
            except Exception as e:
                logger.debug(f'规范化 file URI 失败，保留原值: {e}')
                return image_str.replace('\\', '/')
        try:
            return Path(image_str).resolve().as_uri()
        except Exception as e:
            logger.debug(f'规范化本地图片路径失败，保留原值: {e}')
            return image_str.replace('\\', '/')

    def _extract_content_from_result(self, result: Any) -> dict[str, Any]:
        """
        从执行结果中提取内容（文本和图片）

        Args:
            result: 执行结果（可能是MessageEventResult或其他类型）

        Returns:
            包含 texts、images 和 image_components 的字典
        """
        texts = []
        images = []
        image_components = []
        try:
            if hasattr(result, 'chain') and result.chain:
                pending_components = list(result.chain)
                while pending_components:
                    comp = pending_components.pop(0)
                    if isinstance(comp, Node):
                        pending_components[0:0] = list(comp.content or [])
                        continue
                    if isinstance(comp, Nodes):
                        nested_components = []
                        for node in comp.nodes or []:
                            nested_components.extend(node.content or [])
                        pending_components[0:0] = nested_components
                        continue
                    if isinstance(comp, Plain) and comp.text:
                        texts.append(str(comp.text))
                    elif hasattr(comp, 'type') and comp.type == 'Plain':
                        if hasattr(comp, 'text'):
                            texts.append(str(comp.text))
                    elif isinstance(comp, Image) or (hasattr(comp, 'type') and comp.type == 'Image'):
                        image_components.append(comp)
                        if hasattr(comp, 'url') and comp.url:
                            normalized = self._normalize_image_output(comp.url)
                            if normalized:
                                images.append(normalized)
                        elif hasattr(comp, 'file') and comp.file:
                            normalized = self._normalize_image_output(comp.file)
                            if normalized:
                                images.append(normalized)
            elif isinstance(result, str):
                texts.append(result)
            elif hasattr(result, 'result_message') and result.result_message:
                texts.append(str(result.result_message))
        except Exception as e:
            logger.debug(f'提取内容时出错: {e}')
        return {'texts': texts, 'images': images, 'image_components': image_components}

    def _prepare_results_for_platform(self, results: list[Any], platform_name: str) -> list[Any]:
        """Normalize unsupported result components for the target platform.

        Args:
            results: Results produced by the target command handler.
            platform_name: AstrBot platform adapter name.

        Returns:
            Message results safe to pass to the platform event's send method.
        """
        normalized_results = [MessageEventResult(chain=[Plain(text=result)]) if isinstance(result, str) else result for result in results]
        if platform_name not in QQ_OFFICIAL_PLATFORMS:
            return normalized_results
        prepared_results = []
        for result in normalized_results:
            chain = getattr(result, 'chain', None)
            if chain is None:
                prepared_results.append(result)
                continue
            flattened_components = []
            for comp in chain:
                if isinstance(comp, Reply):
                    continue
                if isinstance(comp, Node):
                    if comp.name:
                        flattened_components.append(Plain(text=f'{comp.name}:\n'))
                    flattened_components.extend(comp.content or [])
                    continue
                if isinstance(comp, Nodes):
                    for index, node in enumerate(comp.nodes or []):
                        if index > 0 and flattened_components:
                            flattened_components.append(Plain(text='\n'))
                        if node.name:
                            flattened_components.append(Plain(text=f'{node.name}:\n'))
                        flattened_components.extend(node.content or [])
                    continue
                flattened_components.append(comp)
            current_components = []
            current_text_length = 0
            chunks = []

            def flush_current() -> None:
                nonlocal current_components, current_text_length
                if current_components:
                    chunks.append(current_components)
                    current_components = []
                    current_text_length = 0
            for comp in flattened_components:
                if not isinstance(comp, Plain):
                    current_components.append(comp)
                    continue
                remaining_text = comp.text or ''
                while remaining_text:
                    available = QQ_OFFICIAL_TEXT_LIMIT - current_text_length
                    if available <= 0:
                        flush_current()
                        available = QQ_OFFICIAL_TEXT_LIMIT
                    if len(remaining_text) <= available:
                        current_components.append(Plain(text=remaining_text))
                        current_text_length += len(remaining_text)
                        remaining_text = ''
                        continue
                    split_at = remaining_text.rfind('\n', 0, available + 1)
                    if split_at < max(1, available // 2):
                        split_at = available
                    else:
                        split_at += 1
                    current_components.append(Plain(text=remaining_text[:split_at]))
                    current_text_length += split_at
                    remaining_text = remaining_text[split_at:]
                    flush_current()
            flush_current()
            derive = getattr(result, 'derive', None)
            for components in chunks:
                if callable(derive):
                    prepared_results.append(derive(components))
                else:
                    prepared_results.append(MessageEventResult(chain=components))
        return prepared_results

    async def _build_llm_tool_result(self, response: dict[str, Any], image_components: list[Any]) -> CallToolResult:
        """构建可回传给 LLM 的工具结果，包含文本和图片内容。"""
        contents = [TextContent(type='text', text=json.dumps(response, ensure_ascii=False))]
        for idx, comp in enumerate(image_components):
            try:
                image_base64 = await comp.convert_to_base64()
                image_file = getattr(comp, 'file', '') or getattr(comp, 'url', '') or getattr(comp, 'path', '') or ''
                mime_type, _ = mimetypes.guess_type(str(image_file))
                if not mime_type or not mime_type.startswith('image/'):
                    mime_type = 'image/png'
                contents.append(ImageContent(type='image', data=image_base64, mimeType=mime_type))
            except Exception as e:
                logger.warning(f'转换指令返回图片给LLM失败 (index={idx}): {e}')
        return CallToolResult(content=contents)

    def _build_message_components(self, command: str, args: str='', at_qq_list: list[str]=None, reply_image_url: str=None) -> list:
        """
        构建消息组件列表，用于设置 event.message_obj

        支持两种 @ 位置模式：
        1. 如果 args 中包含占位符（如 @0, @1），则在对应位置插入 At 组件
        2. 否则，在指令后、参数前插入所有 At 组件（传统模式）

        Args:
            command: 指令名
            args: 指令参数，可以包含 @0, @1 等占位符指定 At 位置
            at_qq_list: 需要@的平台用户 ID 列表（QQ 官方 Bot 使用 OpenID）
            reply_image_url: 需要引用的图片URL

        Returns:
            消息组件列表
        """
        components = []
        if reply_image_url:
            image_source = str(reply_image_url).strip()
            try:
                if image_source.startswith(('http://', 'https://')):
                    img_comp = Image.fromURL(image_source)
                else:
                    img_comp = Image(file=image_source)
            except Exception as e:
                logger.warning(f'按首选方式构建图片组件失败，尝试回退: {e}')
                try:
                    img_comp = Image(file=image_source)
                except Exception:
                    img_comp = Image.fromURL(image_source)
            reply_chain = [img_comp]
            reply_comp = Reply(id=0, sender_id=0, chain=reply_chain)
            components.append(reply_comp)
        if at_qq_list and args:
            has_placeholders = False
            for i in range(len(at_qq_list)):
                if f'@{i}' in args:
                    has_placeholders = True
                    break
            if has_placeholders:
                components.append(Plain(text=f'/{command}'))
                arg_parts = args.split()
                text_buffer = []
                for part in arg_parts:
                    if part.startswith('@') and len(part) > 1 and part[1:].isdigit():
                        idx = int(part[1:])
                        if 0 <= idx < len(at_qq_list):
                            try:
                                if text_buffer:
                                    components.append(Plain(text=' ' + ' '.join(text_buffer)))
                                    text_buffer = []
                                components.append(At(qq=str(at_qq_list[idx])))
                            except Exception as e:
                                logger.warning(f'添加 At 组件失败 (QQ: {at_qq_list[idx]}): {e}')
                                text_buffer.append(part)
                        else:
                            text_buffer.append(part)
                    else:
                        text_buffer.append(part)
                if text_buffer:
                    components.append(Plain(text=' ' + ' '.join(text_buffer)))
            else:
                components.append(Plain(text=f'/{command}'))
                at_position_mode = self._resolve_at_position_mode(command)
                if at_position_mode == 'after_args':
                    if args:
                        components.append(Plain(text=f' {args}'))
                    for qq in at_qq_list:
                        try:
                            components.append(At(qq=str(qq)))
                        except Exception as e:
                            logger.warning(f'添加 At 组件失败 (QQ: {qq}): {e}')
                else:
                    for qq in at_qq_list:
                        try:
                            components.append(At(qq=str(qq)))
                        except Exception as e:
                            logger.warning(f'添加 At 组件失败 (QQ: {qq}): {e}')
                    if args:
                        components.append(Plain(text=f' {args}'))
        else:
            command_text = f'/{command}'
            if args:
                command_text += f' {args}'
            components.append(Plain(text=command_text))
            if at_qq_list:
                for qq in at_qq_list:
                    try:
                        components.append(At(qq=str(qq)))
                    except Exception as e:
                        logger.warning(f'添加 At 组件失败 (QQ: {qq}): {e}')
        return components

    def _build_handler_call(self, handler: Any, event: Any, args_str: str):
        """根据 handler(event, ...) 的签名，把 args_str 尽量解析成真正的形参。

        规则（偏保守）：
        - event 后面的每个参数：
          - 标注为 int/float/bool：吃 1 个 token 并做类型转换
          - 其他（含 str/Union/未标注）：默认把“剩余 token”合并成一个字符串（更符合改名/公告这类指令）
        """
        try:
            sig = inspect.signature(handler)
            params = list(sig.parameters.values())
            if not params:
                return ([event], {})
            tokens = args_str.split() if args_str else []
            pos_args = [event]
            kw_args = {}
            i = 1
            t = 0
            while i < len(params):
                p = params[i]
                if p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                    if t < len(tokens):
                        pos_args.append(' '.join(tokens[t:]))
                    break
                ann = p.annotation
                ann_s = str(ann)

                def is_int_ann():
                    return ann is int or ('int' in ann_s and 'float' not in ann_s and ('bool' not in ann_s) and ('str' not in ann_s))

                def is_float_ann():
                    return ann is float or ('float' in ann_s and 'str' not in ann_s)

                def is_bool_ann():
                    return ann is bool or ('bool' in ann_s and 'str' not in ann_s)
                if t >= len(tokens):
                    break
                tok = tokens[t]
                if is_int_ann():
                    if re.fullmatch('[+-]?\\d+', tok):
                        pos_args.append(int(tok))
                        t += 1
                    else:
                        break
                elif is_float_ann():
                    if re.fullmatch('[+-]?(?:\\d+(?:\\.\\d*)?|\\.\\d+)', tok):
                        pos_args.append(float(tok))
                        t += 1
                    else:
                        break
                elif is_bool_ann():
                    vv = tok.strip().lower()
                    mapping = {'true': True, 'false': False, '1': True, '0': False, 'yes': True, 'no': False, 'on': True, 'off': False, '开启': True, '关闭': False, '开': True, '关': False, '是': True, '否': False}
                    if vv in mapping:
                        pos_args.append(mapping[vv])
                        t += 1
                    else:
                        break
                else:
                    pos_args.append(' '.join(tokens[t:]))
                    t = len(tokens)
                i += 1
            return (pos_args, kw_args)
        except Exception:
            return ([event], {})

    def _coerce_first_arg_by_annotation(self, handler: Any, args: str) -> str:
        """把 args 的第一个 token 按 handler 签名尽量转成 int/float/bool。

        解决执行器只传字符串，导致下游用 isinstance(x, int/float/bool) 判断失败的问题。
        """
        try:
            if not args:
                return args
            tokens = args.split()
            if not tokens:
                return args
            sig = inspect.signature(handler)
            params = list(sig.parameters.values())
            if len(params) < 2:
                return args
            ann = params[1].annotation
            if ann is inspect._empty:
                return args
            s = str(ann)
            kind = None
            if ann is int or ('int' in s and 'float' not in s and ('bool' not in s) and ('str' not in s)):
                kind = 'int'
            elif ann is float or ('float' in s and 'str' not in s):
                kind = 'float'
            elif ann is bool or ('bool' in s and 'str' not in s):
                kind = 'bool'
            else:
                return args
            v = tokens[0]
            if kind == 'int':
                if not re.fullmatch('[+-]?\\d+', v):
                    return args
                tokens[0] = str(int(v))
            elif kind == 'float':
                if not re.fullmatch('[+-]?(?:\\d+(?:\\.\\d*)?|\\.\\d+)', v):
                    return args
                tokens[0] = str(float(v))
            elif kind == 'bool':
                vv = v.strip().lower()
                mapping = {'true': True, 'false': False, '1': True, '0': False, 'yes': True, 'no': False, 'on': True, 'off': False, '开启': True, '关闭': False, '开': True, '关': False, '是': True, '否': False}
                if vv not in mapping:
                    return args
                tokens[0] = 'true' if mapping[vv] else 'false'
            return ' '.join(tokens)
        except Exception:
            return args

    async def execute_command(self, event: AstrMessageEvent, **kwargs) -> CallToolResult:
        """🎮 执行 AstrBot 指令

        当用户明确要求 Bot 帮忙使用某个 AstrBot 功能时，使用此工具执行对应指令。
        用户手动执行时应使用当前会话配置的指令前缀。
        必须先确定指令是否存在，先使用 search_command 查询。

        【普通非管理员指令】
        - 普通指令可以按用户意图正常执行。
        - 用户让你“帮我执行”时，默认使用 as_bot=false，表示代理用户执行。
        - 用户明确让“你自己执行 / 你也执行 / 用你的身份执行”时，可以使用 as_bot=true。

        【管理员权限指令】
        - 管理员指令必须谨慎执行。
        - 非管理员用户请求管理员指令时，只有在用户可信、目标明确、操作低风险，并且用户明确希望 Bot 自己处理时，才可以使用 as_bot=true。
        - as_bot=true 会尝试以 Bot 自己身份执行；只有 Bot 的账号已被配置为 AstrBot 框架管理员时，管理员指令才可能通过权限校验。
        - 高影响管理员指令默认禁止自动执行，即使用户关系很好也不要调用，例如：重启、关闭、停止、更新、重载、删除、清空、重置、权限变更、添加/删除管理员、群发、广播、@全体等。

        【使用场景】
        - 用户说"帮我钓鱼" → execute_command(command="钓鱼", as_bot=false)
        - 用户说"你也去钓鱼吧" → execute_command(command="钓鱼", as_bot=true)
        - 目标插件支持当前平台时，可使用 at_user_id_list 传入目标用户 ID。
        - 用户说"帮我重启机器人" → 不要调用此工具，应该拒绝自动执行

        【特殊参数支持】
        - at_user_id_list: 当指令需要@目标用户时使用。OneBot 传 QQ 号，QQ 官方 Bot 传 member/user OpenID。
        - at_qq_list: at_user_id_list 的旧兼容参数名。
        - reply_image_url: 当指令需要引用图片时使用（如设置群头像、裁剪图片等）

        【注意事项】
        - 指令名不需要带前缀（如 / 或 ~）。
        - 如果指令需要参数，在 args 中传入。
        - 用户表达模糊时，先确认意图；不要为了省事直接执行最接近的指令。
        - 如果目标用户、时间、数量、图片等关键参数不明确，先追问，不要猜测执行。
        - 执行器支持当前平台，不代表目标指令插件也支持；平台不兼容时不要尝试绕过校验。
        - 不确定是否为高影响管理员指令时，不要执行。
        - 工具执行后的 result/images 会返回给你，用于向用户总结结果。

        Args:
            command(string): 要执行的指令名（不含前缀），如 "钓鱼"、"签到"、"背包"
            args(string): 指令参数，多个参数用空格分隔。可以使用 @0, @1 等占位符指定 at_user_id_list 中对应用户的位置（可选）。
            at_user_id_list(array[string]): 需要@的平台用户 ID 列表（可选）。QQ 官方 Bot 需要传 OpenID
            at_qq_list(array[string]): at_user_id_list 的兼容别名（可选）
            reply_image_url(string): 需要引用的图片URL（可选）
            as_bot(boolean): 是否以Bot自己的身份执行（可选，默认false）。false=代理用户执行；true=Bot自己执行。普通指令可按用户意图使用；管理员指令只有在可信、明确、低风险且Bot是框架管理员时才可使用true

        Returns:
            JSON 格式的执行结果，包含 success、command、result/images 或 error 字段
        """
        original_event = event
        event = self._unwrap_message_event(event)
        if event is None:
            logger.warning(f'execute_command 无法从工具上下文提取消息事件: {self._describe_event_like(original_event)}')
            return json.dumps({'success': False, 'error': f'无法从工具上下文识别消息事件，收到对象类型: {self._describe_event_like(original_event)}'}, ensure_ascii=False)
        command = str(kwargs.get('command') or '').strip()
        args = str(kwargs.get('args') or '').strip()
        at_qq_list = kwargs.get('at_user_id_list')
        if at_qq_list is None:
            at_qq_list = kwargs.get('at_qq_list', [])
        if isinstance(at_qq_list, (str, int)):
            at_qq_list = [str(at_qq_list)]
        else:
            at_qq_list = [str(user_id) for user_id in at_qq_list or []]
        reply_image_url = str(kwargs.get('reply_image_url') or '').strip()
        # 模型偶尔把布尔值写成字符串，"false" 不能当成真值，否则会以 Bot 身份执行
        as_bot = kwargs.get('as_bot', False)
        if isinstance(as_bot, str):
            as_bot = as_bot.strip().lower() == 'true'
        log_parts = [f'LLM请求执行指令: {command}']
        if args:
            log_parts.append(f'参数: {args}')
        if at_qq_list:
            log_parts.append(f'@用户: {at_qq_list}')
        if reply_image_url:
            log_parts.append(f'引用图片: {reply_image_url}')
        if as_bot:
            log_parts.append('身份: Bot自己')
        else:
            log_parts.append('身份: 代理用户')
        logger.info(' | '.join(log_parts))
        if not command:
            return json.dumps({'success': False, 'error': '缺少必需参数: command'}, ensure_ascii=False)
        # 每次按当前注册表重建，冷启动时后加载的插件、热重载、WebUI 里禁用的指令才能跟上
        self._build_handler_cache()
        can_exec, reason = self._can_execute(command, event, as_bot=as_bot)
        if not can_exec:
            logger.warning(f'指令执行被拒绝: {command} - {reason}')
            return json.dumps({'success': False, 'error': reason}, ensure_ascii=False)
        command = command.lstrip('/~～')
        actual_command = self._alias_to_command.get(command, command)
        handler_info = self._handler_cache.get(actual_command)
        if not handler_info:
            return json.dumps({'success': False, 'error': f'未找到指令: {command}'}, ensure_ascii=False)
        plugin_instance = self._get_plugin_instance(handler_info['module_path'])
        if not plugin_instance:
            return json.dumps({'success': False, 'error': f'无法获取指令 {command} 所属插件的实例'}, ensure_ascii=False)
        base_event = event
        event_state = self._snapshot_event_state(base_event)
        original_result = base_event.get_result() if hasattr(base_event, 'get_result') else None
        result_texts = []
        result_images = []
        result_image_components = []
        results_to_send = []
        handler: StarHandlerMetadata = handler_info['handler']
        exec_event = base_event
        try:
            if at_qq_list:
                rendered_args = args or ''
                has_placeholders = any((f'@{i}' in rendered_args for i in range(len(at_qq_list))))
                if has_placeholders:
                    for i, qq in enumerate(at_qq_list):
                        rendered_args = rendered_args.replace(f'@{i}', f'@{qq}')
                    base_event.message_str = f'/{actual_command}' + (f' {rendered_args}' if rendered_args else '')
                else:
                    at_text = ' '.join((f'@{qq}' for qq in at_qq_list))
                    at_position_mode = self._resolve_at_position_mode(actual_command)
                    if at_position_mode == 'after_args':
                        base_event.message_str = f'/{actual_command}' + (f' {args}' if args else '') + f' {at_text}'
                    else:
                        base_event.message_str = f'/{actual_command} {at_text}' + (f' {args}' if args else '')
            else:
                base_event.message_str = f'/{actual_command}' + (f' {args}' if args else '')
            if at_qq_list or reply_image_url:
                components = self._build_message_components(actual_command, args, at_qq_list, reply_image_url)
                if hasattr(base_event, 'message_obj') and hasattr(base_event.message_obj, 'message'):
                    base_event.message_obj.message = components
                    base_event.message_obj.message_str = base_event.message_str
                    logger.debug(f'已构建特殊消息组件: At={(len(at_qq_list) if at_qq_list else 0)}, Image={bool(reply_image_url)}')
                else:
                    logger.warning('无法修改 message_obj，可能不支持此操作')
            elif hasattr(base_event, 'message_obj') and hasattr(base_event.message_obj, 'message_str'):
                base_event.message_obj.message_str = base_event.message_str
            if as_bot:
                original_sender_id = base_event.get_sender_id()
                effective_bot_user_id = self._resolve_bot_user_id(base_event)
                self._apply_bot_identity(base_event, effective_bot_user_id)
                exec_event = BotIdentityEventWrapper(base_event, effective_bot_user_id, self.bot_nickname)
                logger.debug(f'已应用Bot身份，原始ID: {original_sender_id}, Bot ID: {effective_bot_user_id}, Bot昵称: {self.bot_nickname}')
            logger.debug(f'执行指令，消息设置为: {base_event.message_str}')
            filters_ok, filter_reason, parsed_params = self._validate_handler_filters(handler, exec_event)
            if not filters_ok:
                logger.warning(f'指令执行被过滤器拒绝: {command} - {filter_reason}')
                return json.dumps({'success': False, 'command': actual_command, 'error': filter_reason}, ensure_ascii=False)
            # 正常流水线里，处理函数拿到的 message_str 不带唤醒前缀
            base_event.message_str = base_event.message_str.lstrip('/')
            async for result in call_handler(exec_event, handler.handler, **parsed_params):
                current_result = exec_event.get_result() if hasattr(exec_event, 'get_result') else None
                if current_result is not None:
                    results_to_send.append(current_result)
                    extracted = self._extract_content_from_result(current_result)
                    result_texts.extend(extracted['texts'])
                    result_images.extend(extracted['images'])
                    result_image_components.extend(extracted['image_components'])
                    exec_event.clear_result()
                elif result is not None:
                    results_to_send.append(result)
                    extracted = self._extract_content_from_result(result)
                    result_texts.extend(extracted['texts'])
                    result_images.extend(extracted['images'])
                    result_image_components.extend(extracted['image_components'])
            total_text_length = sum((len(text) for text in result_texts))
            platform_name = exec_event.get_platform_name()
            prepared_results = self._prepare_results_for_platform(results_to_send, platform_name)
            use_forward = self.enable_forward and total_text_length > self.forward_threshold and (platform_name == 'aiocqhttp')
            sent_count = 0
            send_errors = []

            async def send_normal_results() -> None:
                nonlocal sent_count
                for command_result in prepared_results:
                    try:
                        await exec_event.send(command_result)
                        sent_count += 1
                        logger.debug('已发送指令结果给用户')
                    except Exception as send_err:
                        send_errors.append(str(send_err))
                        logger.warning(f'发送结果失败: {send_err}')
            if use_forward:
                logger.info(f'文本长度 {total_text_length} 超过阈值 {self.forward_threshold}，使用合并转发')
                try:
                    all_components = []
                    for result in results_to_send:
                        if hasattr(result, 'chain') and result.chain:
                            all_components.extend(result.chain)
                    if all_components:
                        node = Node(uin=exec_event.get_self_id(), name=self.bot_nickname, content=all_components)
                        await exec_event.send(exec_event.chain_result([node]))
                        sent_count += 1
                        logger.debug('已使用合并转发发送指令结果')
                    else:
                        await send_normal_results()
                except Exception as forward_err:
                    logger.error(f'合并转发失败，使用普通方式发送: {forward_err}')
                    send_errors.append(str(forward_err))
                    await send_normal_results()
            else:
                await send_normal_results()
            if prepared_results and sent_count == 0:
                return json.dumps({'success': False, 'command': actual_command, 'error': f'指令已执行，但结果发送失败: {'; '.join(send_errors) or '未知错误'}'}, ensure_ascii=False)
            response = {'success': True, 'command': actual_command, 'args': args if args else None}
            if result_texts:
                response['result'] = '\n'.join(result_texts)
            if result_images:
                response['images'] = result_images
                if not result_texts:
                    response['result'] = f'指令返回了 {len(result_images)} 张图片'
            if not result_texts and (not result_images):
                response['result'] = '指令执行完成（无输出内容）'
            response['executed_as'] = 'bot' if as_bot else 'user'
            if send_errors:
                response['send_warning'] = '; '.join(send_errors)
            logger.info(f'指令执行成功: {command} (身份: {('Bot' if as_bot else '用户')}), 文本: {len(result_texts)}, 图片: {len(result_images)}')
            return await self._build_llm_tool_result(response, result_image_components)
        except Exception as e:
            logger.error(f'执行指令 {command} 时发生错误: {e}', exc_info=True)
            return json.dumps({'success': False, 'command': actual_command, 'error': f'执行失败: {str(e)}'}, ensure_ascii=False)
        finally:
            try:
                self._restore_event_state(base_event, event_state)
                if original_result is not None:
                    base_event.set_result(original_result)
                elif hasattr(base_event, 'clear_result'):
                    base_event.clear_result()
            except Exception as restore_err:
                logger.warning(f'恢复事件状态失败: {restore_err}')

    async def list_executable_commands(self, event: AstrMessageEvent, **kwargs) -> str:
        """📋 列出可执行的指令

        获取当前可以通过 execute_command 执行的指令列表。
        当你不确定某个自然语言请求对应哪个指令，或不确定指令是否存在时，先使用此工具查询。

        【使用原则】
        - 此工具只负责列出当前用户身份下可见、可执行的指令。
        - 普通非管理员指令可以按用户意图正常执行。
        - 管理员权限指令可能不会出现在普通用户列表中；如果用户明确要求 Bot 自己处理管理员事务，execute_command(as_bot=true) 仍会根据 Bot 是否为框架管理员和安全规则再次校验。
        - 即使某个高影响管理员指令出现在列表中，也不代表可以自动执行；重启、关闭、停止、更新、重载、删除、清空、重置、权限变更、群发、广播、@全体等仍默认禁止自动执行。

        【使用场景】
        - 用户问"你能帮我做什么" → 列出可执行的指令
        - 用户问"有哪些功能可以用" → 列出可执行的指令
        - 用户提出自然语言请求但你不知道指令名 → 先查询再选择合适指令

        Args:
            category(string): 按插件名筛选（可选）

        Returns:
            JSON 格式的可执行指令列表，按插件分组
        """
        original_event = event
        event = self._unwrap_message_event(event)
        if event is None:
            logger.warning(f'list_executable_commands 无法从工具上下文提取消息事件: {self._describe_event_like(original_event)}')
            return json.dumps({'success': False, 'error': f'无法从工具上下文识别消息事件，收到对象类型: {self._describe_event_like(original_event)}'}, ensure_ascii=False)
        category = str(kwargs.get('category') or '').strip()
        logger.info(f'LLM请求列出可执行指令，分类: {category or '全部'}')
        self._build_handler_cache()
        executable_commands = []
        for cmd_name, handler_info in self._handler_cache.items():
            can_exec, _ = self._can_execute(cmd_name, event)
            if not can_exec:
                continue
            if category and category.lower() not in handler_info['plugin'].lower():
                continue
            executable_commands.append({'command': cmd_name, 'description': handler_info['description'], 'plugin': handler_info['plugin'], 'aliases': handler_info['aliases']})
        plugins_dict = {}
        for cmd in executable_commands:
            plugin = cmd['plugin']
            if plugin not in plugins_dict:
                plugins_dict[plugin] = []
            plugins_dict[plugin].append({'command': cmd['command'], 'description': cmd['description'], 'aliases': cmd['aliases']})
        return json.dumps({'success': True, 'total_count': len(executable_commands), 'plugins': plugins_dict}, ensure_ascii=False, indent=2)

    async def test_bot_identity(self, event: AstrMessageEvent):
        """测试Bot身份切换功能 - 使用包装器方法"""
        original_id = event.get_sender_id()
        test_id = 'test_bot_12345'
        try:
            wrapped_event = BotIdentityEventWrapper(event, test_id, self.bot_nickname)
            wrapped_id = wrapped_event.get_sender_id()
            can_access_message_str = hasattr(wrapped_event, 'message_str')
            can_access_send = hasattr(wrapped_event, 'send')
            result = f'🔍 Bot身份测试结果（包装器方法）：\n原始ID: {original_id}\n测试ID: {test_id}\n包装器返回的ID: {wrapped_id}\n修改是否成功: {('✅ 是' if str(wrapped_id) == str(test_id) else '❌ 否')}\n\n包装器功能测试:\n- 可以访问 message_str: {('✅' if can_access_message_str else '❌')}\n- 可以访问 send 方法: {('✅' if can_access_send else '❌')}\n\nBot配置的ID: {self.bot_user_id}\n\n💡 新方法说明：\n现在使用包装器来覆盖 get_sender_id() 方法，\n而不是直接修改事件对象的属性。\n这样可以确保无论事件对象内部如何实现，\n都能正确返回Bot的ID。\n'
            yield event.plain_result(result)
        except Exception as e:
            yield event.plain_result(f'❌ 测试失败: {e}\n{type(e).__name__}: {str(e)}')

    async def refresh_cache(self, event: AstrMessageEvent):
        """手动刷新指令处理器缓存"""
        self._build_handler_cache()
        yield event.plain_result(f'✅ 指令缓存已刷新，共缓存 {len(self._handler_cache)} 个指令')

    async def executor_status(self, event: AstrMessageEvent):
        """查看LLM指令执行器状态"""
        self._build_handler_cache()
        enabled_str = '✅ 已启用' if self.enabled else '❌ 已禁用'
        whitelist_str = ', '.join(self.whitelist) if self.whitelist else '无限制'
        blacklist_str = ', '.join(self.blacklist) if self.blacklist else '无'
        admin_str = '是' if self.allow_admin_commands else '否'
        admin_users_str = ', '.join(self.admin_users) if self.admin_users else '无'
        status_text = f'=== LLM 指令执行器状态 ===\n🔌 启用状态: {enabled_str}\n📝 缓存指令数: {len(self._handler_cache)}\n📋 白名单: {whitelist_str}\n🚫 黑名单: {blacklist_str}\n👑 允许管理员指令: {admin_str}\n👤 管理员用户: {admin_users_str}\n\n【可执行指令统计】'
        plugin_counts = {}
        for handler_info in self._handler_cache.values():
            plugin = handler_info['plugin']
            if plugin not in plugin_counts:
                plugin_counts[plugin] = 0
            plugin_counts[plugin] += 1
        for plugin, count in sorted(plugin_counts.items()):
            status_text += f'\n  • {plugin}: {count} 个指令'
        yield event.plain_result(status_text)

    async def terminate(self) -> None:
        """插件卸载时调用"""
        logger.info('LLM指令执行器插件已卸载')
