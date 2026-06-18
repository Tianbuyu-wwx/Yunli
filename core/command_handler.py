"""云璃人格插件 - 命令处理器

负责处理 AstrBot 框架注册的 @filter.command 命令：
    /云璃 / /云璃语音 / /云璃资料 / /云璃帮助

由 YunliPersonaPlugin 的同名装饰器方法（cmd_yunli / cmd_voice /
cmd_knowledge / cmd_help）委派调用，保持 AstrBot 的命令注册入口在
@register 主类上。

所有共享 state 通过 self.plugin 访问（_send_segmented / _get_* / 关系/persona/qq 等）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from astrbot.api.event import AstrMessageEvent

from .logging_helpers import get_logger, bind_context

if TYPE_CHECKING:
    from ..main import YunliPersonaPlugin


logger = get_logger(__name__)


class YunliCommandHandler:
    """用户命令处理器（/云璃* 系列）

    入口方法（由主插件以装饰器方式调用）：
        - cmd_yunli(event):      /云璃 [内容] - 基本上下文感知对话
        - cmd_voice(event):      /云璃语音 [类型] - 语音台词
        - cmd_knowledge(event):  /云璃资料 [关键词] - 知识库查询
        - cmd_help(event):       /云璃帮助 - 帮助文本
    """

    def __init__(self, plugin: "YunliPersonaPlugin"):
        self.plugin = plugin

    async def cmd_yunli(self, event: AstrMessageEvent):
        """云璃命令 - 获取云璃的回应（含基本上下文感知）"""
        # 指标：cmd_yunli 总耗时
        with self.plugin.metrics.measure("cmd_duration", tag="yunli"):
            async for result in self._cmd_yunli_impl(event):
                yield result

    async def _cmd_yunli_impl(self, event: AstrMessageEvent):
        """cmd_yunli 的实际实现（被 measure() 包裹以统计耗时）"""
        plugin = self.plugin
        message = event.message_str or ""
        # 去除命令前缀
        message = message.replace("/云璃", "").replace("云璃", "", 1).strip()
        group_id = plugin._get_group_id(event)
        user_id = plugin._get_user_id(event)
        # 结构化日志：绑定命令上下文
        bind_context(
            scope=f"cmd:yunli:{group_id}:{user_id}" if group_id or user_id else "cmd:yunli",
            user_id=user_id or None,
            group_id=group_id or None,
        )

        # 注入基本上下文（关系 + 记忆，不阻塞主流程）
        context_meta = ""
        if group_id and user_id:
            try:
                rel_hint = plugin.relationship.get_hint(group_id, user_id)
                if rel_hint:
                    context_meta += f" ({rel_hint.split('，')[0]})"
            except (RuntimeError, KeyError, AttributeError):
                # 类别 C：边界保护，关系提示失败不阻塞主流程
                logger.debug(
                    "cmd_yunli: 获取关系提示失败",
                    exc_info=True,
                    extra={"cmd": "yunli"},
                )

        if not message:
            # 没有参数，返回随机台词
            response = plugin.persona_engine.get_direct_response("你好" + context_meta)
            if not response:
                response = "嗯？叫我有什么事吗？"
        else:
            # 尝试获取直接响应
            response = plugin.persona_engine.get_direct_response(message + context_meta)
            if not response:
                response = f"{message}？这是什么意思？"

        # 润色
        response = plugin.persona_engine.polish_response(response, message)
        response = plugin.qq_behavior.format_for_qq(response)

        # 命令响应统一走分段发送
        async for result in plugin._send_segmented(event, response):
            yield result

    async def cmd_voice(self, event: AstrMessageEvent):
        """获取云璃的语音台词"""
        # 指标：cmd_voice 总耗时
        with self.plugin.metrics.measure("cmd_duration", tag="voice"):
            async for result in self._cmd_voice_impl(event):
                yield result

    async def _cmd_voice_impl(self, event: AstrMessageEvent):
        """cmd_voice 的实际实现（被 measure() 包裹以统计耗时）"""
        plugin = self.plugin
        message = event.message_str or ""
        group_id = plugin._get_group_id(event)
        user_id = plugin._get_user_id(event)
        # 结构化日志：绑定命令上下文
        bind_context(
            scope=f"cmd:voice:{group_id}:{user_id}" if group_id or user_id else "cmd:voice",
            user_id=user_id or None,
            group_id=group_id or None,
        )
        line_type = None

        # 解析参数
        if "战斗" in message or "battle" in message:
            line_type = "battle"
        elif "技能" in message or "skill" in message:
            line_type = "skill"
        elif "胜利" in message or "victory" in message:
            line_type = "victory"
        elif "待机" in message or "idle" in message:
            line_type = "idle"

        voice_line = plugin.persona_engine.get_voice_line(line_type)
        if voice_line:
            response = voice_line
        else:
            response = "哼，现在不想说话。"

        yield event.plain_result(response)

    async def cmd_knowledge(self, event: AstrMessageEvent):
        """查询云璃相关知识"""
        # 指标：cmd_knowledge 总耗时
        with self.plugin.metrics.measure("cmd_duration", tag="knowledge"):
            async for result in self._cmd_knowledge_impl(event):
                yield result

    async def _cmd_knowledge_impl(self, event: AstrMessageEvent):
        """cmd_knowledge 的实际实现（被 measure() 包裹以统计耗时）"""
        plugin = self.plugin
        message = event.message_str or ""
        group_id = plugin._get_group_id(event)
        user_id = plugin._get_user_id(event)
        # 结构化日志：绑定命令上下文
        bind_context(
            scope=f"cmd:knowledge:{group_id}:{user_id}" if group_id or user_id else "cmd:knowledge",
            user_id=user_id or None,
            group_id=group_id or None,
        )
        keyword = message.replace("/云璃资料", "").replace("云璃资料", "", 1).strip()

        if not keyword:
            yield event.plain_result("你要查什么？跟我说说看。")
            return

        # 查询知识库
        knowledge = plugin.db.query_knowledge(keyword, limit=3)

        if knowledge:
            responses = []
            for k in knowledge:
                responses.append(f"【{k['entity_name']}】{k['description']}")
            response = "\n".join(responses)
        else:
            response = f"关于'{keyword}'…我也不太清楚呢。你要不要教教我？"

        # 知识查询结果统一走分段发送
        async for result in plugin._send_segmented(event, response):
            yield result

    async def cmd_help(self, event: AstrMessageEvent):
        """显示帮助信息"""
        # 指标：cmd_help 总耗时
        with self.plugin.metrics.measure("cmd_duration", tag="help"):
            async for result in self._cmd_help_impl(event):
                yield result

    async def _cmd_help_impl(self, event: AstrMessageEvent):
        """cmd_help 的实际实现（被 measure() 包裹以统计耗时）"""
        plugin = self.plugin
        group_id = plugin._get_group_id(event)
        user_id = plugin._get_user_id(event)
        # 结构化日志：绑定命令上下文
        bind_context(
            scope=f"cmd:help:{group_id}:{user_id}" if group_id or user_id else "cmd:help",
            user_id=user_id or None,
            group_id=group_id or None,
        )
        help_text = """【云璃插件帮助】
/云璃 [内容] - 跟云璃说话
/云璃语音 [类型] - 听云璃的台词（战斗/技能/胜利/待机）
/云璃资料 [关键词] - 查询相关知识
/云璃帮助 - 显示本帮助

也可以直接@我，我会回应你的~"""

        yield event.plain_result(help_text)
