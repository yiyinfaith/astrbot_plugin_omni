import collections

import json

from datetime import datetime, timezone

from typing import Any

from astrbot.api import AstrBotConfig, logger

from astrbot.api.event import AstrMessageEvent, filter

from astrbot.api.star import Context, Star

from astrbot.core.provider.entities import ProviderRequest

from astrbot.core.star import command_management

class CommandQueryCore:
    """
    AstrBot 指令查询插件 v2.2.0

    【核心功能】
    为 LLM 提供指令查询能力，让 LLM 能够：
    1. 纠正用户输入的错误指令
    2. 引导用户正确使用功能
    3. 推荐相关指令

    【应用场景】
    - 用户输错指令时，LLM 查询正确写法并纠正
    - 用户不知道怎么用时，LLM 查询用法并引导
    - 用户找功能时，LLM 搜索相关指令并推荐

    【实现要点】
    - 使用 AstrBot 指令管理接口读取当前生效指令
    - 跟随后台重命名、禁用、别名和权限配置
    - 别名只用于匹配，结果聚合到主命令
    """

    def __init__(self, context: Context, config: AstrBotConfig=None):
        """插件初始化"""
        self.context = context
        self.config = config
        self.command_prefix = config.get('command_prefix', '/') if config else '/'
        logger.info(f'指令查询插件已加载 v2.2.0 (指令前缀: {self.command_prefix})')

    def _replace_prefix(self, command: str) -> str:
        """
        将指令中的 / 前缀替换为用户配置的前缀

        Args:
            command: 原始指令（如 "/钓鱼"）

        Returns:
            替换后的指令（如 "~钓鱼"）
        """
        if command.startswith('/'):
            return self.command_prefix + command[1:]
        return command

    def _normalize_command(self, command: str) -> str:
        """标准化为内部使用的 /command 形式。"""
        command = ' '.join((command or '').strip().split())
        if not command:
            return ''
        if command.startswith(self.command_prefix) and self.command_prefix != '/':
            command = '/' + command[len(self.command_prefix):].lstrip()
        if not command.startswith('/'):
            command = '/' + command
        return command

    def _unwrap_message_event(self, event_or_context: Any) -> AstrMessageEvent | None:
        """Extract the real message event from LLM tool context wrappers.

        Args:
            event_or_context: A direct message event or a nested tool context.

        Returns:
            The message event containing a unified message origin, or None when
            no valid event can be found.
        """
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
            if getattr(candidate, 'unified_msg_origin', None):
                return candidate
            inner_context = getattr(candidate, 'context', None)
            if inner_context is not None:
                candidates.append(getattr(inner_context, 'event', None))
                candidates.append(inner_context)
            candidates.append(getattr(candidate, 'event', None))
        return None

    def _compose_alias(self, parent_signature: str, alias: str) -> str:
        alias = ' '.join((alias or '').strip().split())
        parent_signature = ' '.join((parent_signature or '').strip().split())
        if not alias:
            return ''
        if parent_signature and (not alias.startswith(parent_signature + ' ')):
            return f'{parent_signature} {alias}'
        return alias

    def _iter_command_items(self, items: list[dict[str, Any]]):
        for item in items:
            yield item
            sub_commands = item.get('sub_commands') or []
            if sub_commands:
                yield from self._iter_command_items(sub_commands)

    def _get_active_module_paths(self) -> set[str]:
        try:
            return {star.module_path for star in self.context.get_all_stars() if star.activated and star.module_path}
        except Exception as e:
            logger.error(f'获取激活插件列表失败: {e}')
            return set()

    async def _get_all_commands(self, event: Any) -> dict[str, dict]:
        """获取当前事件可用的指令信息。

        使用 AstrBot 的 command_management.list_commands() 作为数据源，确保后台重命名、
        禁用、别名和权限配置都会被应用。别名只作为主命令的匹配字段，不作为独立结果返回。

        Args:
            event: 当前消息事件或工具上下文，用于按会话读取对应配置的插件选择范围。

        Returns:
            以标准化命令名为键的可用指令信息。
        """
        commands_dict = {}
        try:
            command_items = await command_management.list_commands()
        except Exception as e:
            logger.error(f'获取指令列表失败: {e}')
            return {}
        active_module_paths = self._get_active_module_paths()
        message_event = self._unwrap_message_event(event)
        if message_event is None:
            error = '无法从工具上下文提取当前消息事件'
            logger.error(error)
            raise ValueError(error)
        try:
            current_config = self.context.get_config(message_event.unified_msg_origin)
            plugin_set = current_config.get('plugin_set', ['*'])
        except Exception as e:
            logger.error(f'获取当前会话配置失败: {e}')
            raise
        selected_plugins = None if not isinstance(plugin_set, list) or '*' in plugin_set else {str(plugin_name) for plugin_name in plugin_set}
        for item in self._iter_command_items(command_items):
            if not item.get('enabled', True):
                continue
            module_path = item.get('module_path', '')
            if module_path not in active_module_paths:
                continue
            plugin_name = item.get('plugin') or '未知插件'
            if selected_plugins is not None and (not item.get('reserved', False)) and (plugin_name not in selected_plugins):
                continue
            if plugin_name in ['astrbot', 'astrbot_plugin_command_query', 'astrbot-reminder']:
                continue
            command = item.get('effective_command') or item.get('original_command') or item.get('handler_name') or ''
            primary_command = self._normalize_command(command)
            if not primary_command:
                continue
            aliases = []
            parent_signature = item.get('parent_signature') or ''
            for alias in item.get('aliases') or []:
                normalized_alias = self._normalize_command(self._compose_alias(parent_signature, str(alias)))
                if normalized_alias and normalized_alias != primary_command:
                    aliases.append(normalized_alias)
            commands_dict[primary_command] = {'command': primary_command, 'description': item.get('description') or '无描述', 'plugin': plugin_name, 'plugin_display_name': item.get('plugin_display_name'), 'aliases': sorted(set(aliases)), 'is_admin': item.get('permission') == 'admin', 'command_id': item.get('handler_full_name') or primary_command, 'command_type': item.get('type') or 'command'}
        logger.info(f'已读取 {len(commands_dict)} 个当前生效指令（别名已聚合）')
        return commands_dict

    async def _search_similar_commands(self, event: AstrMessageEvent, keyword: str, limit: int=5) -> list[dict]:
        """搜索当前事件可用的相似指令。

        别名用于匹配，但结果始终返回主命令，避免别名成为重复候选。

        Args:
            event: 当前消息事件，用于限定可用插件范围。
            keyword: 指令名、别名、描述或插件名关键词。
            limit: 最多返回的匹配数量。

        Returns:
            按匹配优先级排列的指令信息。
        """
        all_commands = await self._get_all_commands(event)
        keyword_lower = keyword.lower().strip()
        if self.command_prefix != '/' and keyword_lower.startswith(self.command_prefix):
            keyword_lower = keyword_lower[len(self.command_prefix):].strip()
        if keyword_lower.startswith('/'):
            keyword_lower = keyword_lower[1:].strip()
        results = []
        seen = set()

        def add_result(cmd_info: dict, matched_alias: str=''):
            command_id = cmd_info.get('command_id') or cmd_info['command']
            if command_id in seen:
                return
            seen.add(command_id)
            if matched_alias:
                cmd_info = {**cmd_info, 'matched_alias': matched_alias}
            results.append(cmd_info)
        exact_match = f'/{keyword_lower}'
        if exact_match in all_commands:
            add_result(all_commands[exact_match])
        for cmd_info in all_commands.values():
            for alias in cmd_info['aliases']:
                if exact_match == alias.lower():
                    add_result(cmd_info, matched_alias=alias)
        for cmd_name, cmd_info in all_commands.items():
            cmd_name_lower = cmd_name.lower()
            if keyword_lower in cmd_name_lower:
                add_result(cmd_info)
                continue
            for alias in cmd_info['aliases']:
                if keyword_lower in alias.lower():
                    add_result(cmd_info, matched_alias=alias)
                    break
        if len(results) < limit:
            for cmd_info in all_commands.values():
                desc_lower = cmd_info['description'].lower()
                if keyword_lower in desc_lower:
                    add_result(cmd_info)
                if len(results) >= limit:
                    break
        if len(results) < limit:
            for cmd_info in all_commands.values():
                plugin_lower = cmd_info['plugin'].lower()
                if keyword_lower in plugin_lower:
                    add_result(cmd_info)
                if len(results) >= limit:
                    break
        return results[:limit]

    def _find_command(self, all_commands: dict[str, dict], command_name: str) -> dict | None:
        """按主命令或别名查找，命中别名时仍返回主命令信息。"""
        if command_name in all_commands:
            return all_commands[command_name]
        for cmd_info in all_commands.values():
            if command_name in cmd_info['aliases']:
                return cmd_info
        return None

    async def search_command(self, event: AstrMessageEvent, **kwargs) -> str:
        """搜索当前 Bot 配置生效的指令，支持功能词、插件名、指令与别名。

        用户输错指令、寻找指令用法时使用。权限要求由结果的 is_admin 标明。
        查询插件是否安装、启用、当前配置有哪些插件或无指令功能时，使用
        get_plugin_status。没有搜索到指令不代表插件不存在或未启用。
        信息不足时可以继续查询插件状态；详细用法使用 get_command_detail。

        Args:
            keyword(string): 搜索关键词（指令名/功能词/插件名的任意部分）

        Returns:
            JSON格式，包含最多5条匹配的指令（已包含描述和别名）

        示例:
            用户询问钓鱼指令时，使用 search_command(keyword="钓鱼")。
        """
        try:
            keyword = kwargs.get('keyword', '')
            if not keyword:
                return json.dumps({'success': False, 'message': '缺少必需参数: keyword', 'results': []}, ensure_ascii=False, indent=2)
            logger.info(f'LLM搜索指令: {keyword}')
            results = await self._search_similar_commands(event, keyword, limit=5)
            if not results:
                return json.dumps({'success': False, 'message': f"未找到与 '{keyword}' 相关的指令", 'results': []}, ensure_ascii=False, indent=2)
            clean_results = []
            for result in results:
                clean_result = {'command': self._replace_prefix(result['command']), 'description': result['description'], 'plugin': result['plugin'], 'aliases': [self._replace_prefix(alias) for alias in result['aliases']], 'is_admin': result.get('is_admin', False), 'command_id': result.get('command_id', '')}
                if result.get('matched_alias'):
                    clean_result['matched_alias'] = self._replace_prefix(result['matched_alias'])
                clean_results.append(clean_result)
            logger.info(f'找到 {len(clean_results)} 条相关指令')
            return json.dumps({'success': True, 'message': f"找到 {len(clean_results)} 条与 '{keyword}' 相关的指令", 'results': clean_results}, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f'搜索指令时发生错误: {e}')
            return json.dumps({'success': False, 'message': f'搜索失败: {str(e)}', 'results': []}, ensure_ascii=False, indent=2)

    async def get_command_detail(self, event: AstrMessageEvent, **kwargs) -> str:
        """📖 【仅在必要时使用】获取指令详细用法

        ⚠️ 慎用！只在用户明确询问"怎么用"时才调用！

        【何时使用】
        1. 用户明确问"XX指令怎么用"、"XX怎么操作"
        2. 用户问"XX需要什么参数"
        3. 已通过 search_command 找到指令，用户追问具体用法

        【何时不用】
        ❌ 用户只是问"有XX功能吗" → 用 search_command 就够了！
        ❌ 用户问"能不能XX" → 用 search_command 就够了！
        ❌ 已经调用过 search_command → 不要再调用这个！除非用户追问

        【避免浪费】
        - search_command 的结果已经包含描述，通常够用了
        - 只有用户明确需要详细用法时才调用此工具

        Args:
            command_name(string): 指令名（可带或不带前缀）

        返回:
            JSON格式，包含指令详情和相关推荐
        """
        try:
            command_name = kwargs.get('command_name', '')
            if not command_name:
                return json.dumps({'success': False, 'message': '缺少必需参数: command_name'}, ensure_ascii=False, indent=2)
            logger.info(f'LLM查询指令详情: {command_name}')
            command_name = self._normalize_command(command_name)
            all_commands = await self._get_all_commands(event)
            cmd_info = self._find_command(all_commands, command_name)
            if not cmd_info:
                similar = await self._search_similar_commands(event, command_name, limit=3)
                return json.dumps({'success': False, 'message': f"未找到指令 '{command_name}'", 'suggestions': [self._replace_prefix(cmd['command']) for cmd in similar]}, ensure_ascii=False, indent=2)
            plugin_name = cmd_info['plugin']
            similar_commands = []
            for cmd_name, cmd_data in all_commands.items():
                if cmd_data['plugin'] == plugin_name and cmd_name != cmd_info['command']:
                    similar_commands.append(cmd_name)
                    if len(similar_commands) >= 3:
                        break
            result = {'success': True, 'command': self._replace_prefix(cmd_info['command']), 'description': cmd_info['description'], 'plugin': cmd_info['plugin'], 'aliases': [self._replace_prefix(alias) for alias in cmd_info['aliases']], 'is_admin': cmd_info.get('is_admin', False), 'command_id': cmd_info.get('command_id', ''), 'similar_commands': [self._replace_prefix(cmd) for cmd in similar_commands]}
            logger.info(f'成功获取指令详情: {command_name}')
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f'查询指令详情时发生错误: {e}')
            return json.dumps({'success': False, 'message': f'查询失败: {str(e)}'}, ensure_ascii=False, indent=2)

    async def get_plugin_status(self, event: AstrMessageEvent, plugin_name: str='', enabled_only: bool=False) -> str:
        """实时查询当前消息所属 Bot 和配置的插件状态，包括没有指令的插件。

        回答“你启用了哪些插件”“某插件是否启用”“你有什么插件功能”时先调用。
        以当前消息的 Bot 实例与配置为准，不可根据服务器目录、README、旧对话或
        指令搜索结果判断启用状态，也不能把 OneBot 的结果套用到官方 Bot。
        返回全局开关、当前配置选择、实例加载状态和停用原因。没有记录只能说明
        运行时未登记，不能断言服务器未安装。启用不代表每条指令或工具均有执行权限。

        Args:
            plugin_name(string): 可选，插件名称、显示名称或目录名；空字符串查询清单。
            enabled_only(boolean): 可选，默认 false；清单查询传 true 仅列当前配置启用的插件。指定插件名称时总是返回其状态。

        Returns:
            JSON 格式的当前 Bot、配置标识、查询时间和插件状态。
        """
        try:
            if not isinstance(plugin_name, str) or not isinstance(enabled_only, bool):
                raise ValueError('plugin_name 必须是字符串，enabled_only 必须是布尔值')
            message_event = self._unwrap_message_event(event)
            if message_event is None:
                raise ValueError('无法从工具上下文提取当前消息事件')
            umo = message_event.unified_msg_origin
            config = self.context.get_config(umo)
            info = self.context.astrbot_config_mgr.get_conf_info(umo)
            scope = {'platform_id': message_event.get_platform_id(), 'platform_type': message_event.get_platform_name(), 'config_id': info['id'], 'config_name': info['name']}
            plugin_set = config.get('plugin_set', ['*'])
            if not isinstance(plugin_set, list) or any((not isinstance(name, str) for name in plugin_set)):
                raise ValueError('当前配置的 plugin_set 无效，无法确认插件状态')
            selected = None if '*' in plugin_set else set(plugin_set)
            plugins = []
            for star in self.context.get_all_stars():
                name = star.name or star.root_dir_name or '未知插件'
                globally_enabled = bool(star.activated)
                selected_in_config = bool(star.reserved or selected is None or name in selected)
                instance_loaded = star.star_cls is not None
                if not globally_enabled:
                    status, reason = ('disabled_globally', '插件已被全局禁用')
                elif not instance_loaded:
                    status, reason = ('instance_unavailable', '运行时插件实例不可用')
                elif not selected_in_config:
                    status, reason = ('not_selected', '当前 Bot 配置未选择此插件')
                else:
                    status, reason = ('enabled', '全局启用、实例已加载且当前配置允许')
                plugins.append({'name': name, 'display_name': star.display_name or name, 'directory_name': star.root_dir_name, 'version': star.version, 'description': (star.short_desc or star.desc or '')[:240], 'globally_enabled': globally_enabled, 'selected_in_current_config': selected_in_config, 'instance_loaded': instance_loaded, 'enabled_for_current_bot': status == 'enabled', 'status': status, 'reason': reason})
            manager = getattr(self.context, '_star_manager', None)
            failures = getattr(manager, 'failed_plugin_dict', None)
            failure_registry_available = isinstance(failures, dict)
            registered_directories = {item['directory_name'] for item in plugins}
            for directory, record in (failures or {}).items():
                if directory in registered_directories:
                    continue
                name = record.get('name') or directory
                plugins.append({'name': name, 'display_name': record.get('display_name') or name, 'directory_name': directory, 'version': record.get('version'), 'description': (record.get('short_desc') or record.get('desc') or '')[:240], 'globally_enabled': None, 'selected_in_current_config': bool(record.get('reserved') or selected is None or name in selected), 'instance_loaded': False, 'enabled_for_current_bot': False, 'status': 'load_failed', 'reason': '插件加载失败，详细原因请管理员查看后台日志'})
            plugins.sort(key=lambda item: item['name'].casefold())
            total = len(plugins)
            enabled_count = sum((item['enabled_for_current_bot'] for item in plugins))
            keyword = ''.join((char for char in plugin_name.strip().casefold() if char.isalnum()))
            if plugin_name.strip() and (not keyword):
                raise ValueError('插件名称必须包含文字或数字')
            if keyword:
                matches = []
                exact = []
                for item in plugins:
                    identifiers = [''.join((char for char in value.casefold() if char.isalnum())) for value in (item['name'], item['display_name'], item['directory_name']) if value]
                    if keyword in identifiers:
                        exact.append(item)
                    elif any((keyword in value for value in identifiers)):
                        matches.append(item)
                plugins = exact or matches
            elif enabled_only:
                plugins = [item for item in plugins if item['enabled_for_current_bot']]
            return json.dumps({'success': True, 'scope': scope, 'observed_at': datetime.now(timezone.utc).isoformat(), 'source': 'runtime_registry_and_current_session_config', 'failure_registry_available': failure_registry_available, 'runtime_plugin_count': total, 'enabled_in_current_config_count': enabled_count, 'matched': bool(plugins) if keyword else None, 'count': len(plugins), 'plugins': plugins, 'note': '运行时未找到该插件，安装情况未知；不要根据目录或旧对话猜测。' if keyword and (not plugins) else '状态仅适用于上述 Bot 与配置；启用不保证所有指令、工具或平台功能均可执行。'}, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f'查询插件状态失败: {e}')
            return json.dumps({'success': False, 'message': f'查询失败: {e}', 'plugins': []}, ensure_ascii=False)

    async def remind_plugin_status(self, event: AstrMessageEvent, request: ProviderRequest) -> None:
        """Remind the model to query plugin state for the current bot configuration.

        Args:
            event: The message event identifying the active bot and configuration.
            request: The pending request whose available tools determine the reminder.
        """
        if not request.func_tool or not any((tool.name == 'get_plugin_status' for tool in request.func_tool.tools)):
            return
        try:
            info = self.context.astrbot_config_mgr.get_conf_info(event.unified_msg_origin)
            scope = json.dumps({'platform_id': event.get_platform_id(), 'platform_type': event.get_platform_name(), 'config_id': info['id'], 'config_name': info['name']}, ensure_ascii=False)
            marker = '[command_query_plugin_status]'
            if marker not in (request.system_prompt or ''):
                request.system_prompt = (request.system_prompt or '') + f'\n\n{marker}\n当前消息所属 Bot 和配置：{scope}。回答插件是否启用、插件清单或自身插件功能前，调用 get_plugin_status获取实时状态；列当前启用插件时传 enabled_only=true。目录存在、README 内容、没有找到指令或旧对话都不能证明启用状态。结果只适用于当前 Bot 与配置，不可套用到其他 Bot。'
        except Exception as e:
            logger.warning(f'无法添加插件状态查询提示: {e}')

    async def list_plugin_commands(self, event: AstrMessageEvent, **kwargs) -> str:
        """列出当前 Bot 配置中有生效指令的插件，或指定插件的指令。

        这是指令目录，可能遗漏没有指令的插件，不是完整插件清单。
        查询完整清单、启用状态或插件功能说明时使用 get_plugin_status。
        单条指令搜索使用 search_command；详细用法使用 get_command_detail。

        Args:
            plugin_name(string): 可选，不传列出当前配置有生效指令的插件；传入列出该插件的指令。

        Returns:
            JSON格式的插件列表或插件指令列表

        示例:
            用户询问音乐插件的指令时，使用 list_plugin_commands(plugin_name="music")。
        """
        try:
            plugin_name = kwargs.get('plugin_name', '')
            logger.info(f'LLM查询插件指令: {plugin_name or '所有插件'}')
            all_commands = await self._get_all_commands(event)
            plugins_dict = collections.defaultdict(list)
            for cmd_info in all_commands.values():
                plugins_dict[cmd_info['plugin']].append(cmd_info)
            if not plugin_name:
                plugin_list = sorted(plugins_dict.keys())
                return json.dumps({'success': True, 'message': f'当前 Bot 配置中有 {len(plugin_list)} 个插件提供生效指令', 'scope': 'current_session_config_command_catalog', 'complete_plugin_inventory': False, 'plugins': plugin_list, 'hint': '此列表不包含无指令插件；完整清单和启用状态请调用 get_plugin_status。指定 plugin_name 可查看指令。'}, ensure_ascii=False, indent=2)
            plugin_name_lower = plugin_name.lower()
            matched_plugin = None
            for pname in plugins_dict.keys():
                if plugin_name_lower in pname.lower():
                    matched_plugin = pname
                    break
            if not matched_plugin:
                return json.dumps({'success': False, 'message': f"当前配置的指令目录中未找到 '{plugin_name}'；请用 get_plugin_status 确认插件状态", 'available_plugins': sorted(plugins_dict.keys())}, ensure_ascii=False, indent=2)
            commands = plugins_dict[matched_plugin]
            result = {'success': True, 'plugin': matched_plugin, 'command_count': len(commands), 'commands': [{'command': self._replace_prefix(cmd['command']), 'description': cmd['description'], 'aliases': [self._replace_prefix(alias) for alias in cmd['aliases']], 'is_admin': cmd.get('is_admin', False), 'command_id': cmd.get('command_id', '')} for cmd in commands]}
            logger.info(f"找到插件 '{matched_plugin}' 的 {len(commands)} 条指令")
            return json.dumps(result, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f'查询插件指令时发生错误: {e}')
            return json.dumps({'success': False, 'message': f'查询失败: {str(e)}'}, ensure_ascii=False, indent=2)

    async def terminate(self) -> None:
        """插件卸载时调用"""
        logger.info('指令查询插件已卸载')
