"""云璃人格插件 - 事件管线

负责 LLM 请求前的人格提示词注入 与 响应后的分段发送/拟人化/Token 监控。
由 YunliPersonaPlugin.on_llm_request / on_llm_response 通过装饰器入口委派调用。

所有跨协作者共享的 state（_background_tasks / _at_detector / _debouncer /
_log_interaction / _safe_create_task / 各种 _get_* 辅助函数 / 配置快捷字段）
均通过 self.plugin 访问，避免循环引用。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional, TYPE_CHECKING

from astrbot.api.event import AstrMessageEvent
from astrbot.api.provider import ProviderRequest, LLMResponse
from astrbot.core.message.message_event_result import MessageChain
from astrbot.api.message_components import Plain

from . import (
    RequestContext,
    estimate_tokens,
    is_structured_summary,
    get_thread_tracker,
)
from .logging_helpers import get_logger, bind_context, reset_bind

if TYPE_CHECKING:
    # 仅类型提示，避免运行时循环引用
    from . import GroupPerception
    from ..main import YunliPersonaPlugin


logger = get_logger(__name__)


def _gp():
    """延迟查找 GroupPerception 类（通过 yunli.main 模块命名空间）

    这样 unittest.mock.patch('yunli.main.GroupPerception') 才能在测试中
    生效（core/event_pipeline.py 不能直接 from . import GroupPerception，
    因为那样会在 event_pipeline 模块命名空间里产生独立绑定，绕过 patcher）。
    """
    from .. import main as _main
    return _main.GroupPerception


class YunliEventPipeline:
    """LLM 请求/响应事件管线

    入口方法（由主插件以装饰器方式调用）：
        - on_request(event, req): LLM 请求前的人格提示词注入
        - on_response(event, response): LLM 响应后的拟人化/分段/Token 监控

    私有方法（仅内部使用）：
        - _on_debounce_flush / _inject_persona_prompt / _do_inject_persona_prompt
        - _batch_get_db_context / _send_remaining_segments*
        - _log_token_usage
    """

    def __init__(self, plugin: "YunliPersonaPlugin"):
        self.plugin = plugin

    # ========== LLM 请求前 ==========

    async def on_message(self, event: AstrMessageEvent):
        """@filter.on_message 装饰的入口（实际执行体）

        P2-12 修复：常驻触发旁听模式。原 on_llm_request 只在 AstrBot
        决定调用 LLM 时触发，导致非@消息实际无法被记录。新增 on_message
        handler，每条消息都尝试记录，不区分是否 @ 云璃。

        处理策略：
        - 非@云璃的消息：完整走旁听记录（日志 + 氛围 + 话题 + 记忆提取）
        - @云璃的消息：只更新线程追踪，不记录日志，避免 on_llm_request 重复记录
        """
        plugin = self.plugin
        is_at_me = plugin._should_activate(event)

        if not is_at_me:
            # 未@云璃：完整旁听记录
            plugin.metrics.increment("msg_processed_total", tag="no_at")
            await self._record_passive_interaction(event)
            return

        # @云璃：只做线程追踪，日志记录留给 on_llm_request
        group_id = plugin._get_group_id(event)
        user_id = plugin._get_user_id(event)
        user_nickname = plugin._get_user_nickname(event)
        message = event.message_str or ""

        if group_id and user_id and message:
            try:
                thread_tracker = get_thread_tracker()
                user_topic = (
                    plugin.persona_engine.language.detect_topic(message)
                    if message else ""
                )
                # 个人线程
                thread_tracker.record_user_message(
                    f"{group_id}:{user_id}", message, user_topic
                )
                # 群级线程
                thread_tracker.record_user_message_to_group(
                    group_id, user_id, user_nickname, message
                )
            except Exception:
                logger.debug("on_message: @消息线程追踪失败", exc_info=True)

    async def on_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """@filter.on_llm_request 装饰的入口（实际执行体）

        由 YunliPersonaPlugin.on_llm_request 装饰器委派调用。
        只处理 @ 云璃的消息，负责提示词注入和日志记录。
        """
        is_at_me = self.plugin._should_activate(event)
        if not is_at_me:
            # on_message 已经处理了非@消息，这里直接跳过
            return

        # 指标：on_request 总耗时（已激活）
        with self.plugin.metrics.measure("on_request_duration", tag="activated"):
            await self._on_request_impl(event, req)

    async def _record_passive_interaction(self, event: AstrMessageEvent):
        """旁听模式：记录非@云璃的群聊互动，用于记忆和群聊感知

        不注入人格提示词、不生成回复，仅：
        - 记录互动日志（trigger_type="passive"，response 为空）
        - 更新群氛围和话题（通过 _log_interaction 回调）
        - 轻量记忆提取（extract_memory_lightweight）
        - 话题线程追踪（update_topic_threads）
        - 短期对话线程追踪（thread_tracker.record_user_message）

        这样云璃能"旁听"群聊，建立群友画像，在被@时能认出群友。
        """
        plugin = self.plugin
        group_id = plugin._get_group_id(event)
        user_id = plugin._get_user_id(event)
        user_nickname = plugin._get_user_nickname(event)
        message = event.message_str or ""

        # 仅处理群聊消息（私聊不旁听）
        if not group_id or not user_id or not message:
            return

        # P2-10 修复：过滤纯表情/图片/转发等非文本消息
        # 这些消息对记忆提取无价值，且会产生噪音记忆
        import re as _re
        # 纯表情消息（QQ 表情如 [表情] 或 CQ 码如 [CQ:image,...]）
        if _re.match(r'^(\[CQ:[a-z,=]+\](\s*)|[\[【].*?[\]】](\s*))+$', message):
            return
        # 纯数字/符号消息（如 "1" "666" "..."）
        if _re.match(r'^[\d\s.。,，!！?？~～\-—+=*/\\|]+$', message):
            return
        # 过短消息（单字符，无信息量）
        if len(message.strip()) <= 1:
            return

        # 跳过命令消息（/云璃 等）
        if message.startswith("/"):
            return

        # 跳过机器人自己的消息
        self_id = plugin._get_cached_self_id()
        if self_id and user_id == self_id:
            return

        # 结构化日志
        bind_context(
            scope=f"passive:{group_id}:{user_id}",
            user_id=user_id or None,
            group_id=group_id or None,
        )

        # 3. 短期对话线程追踪：记录用户消息
        #    这样用户后来@云璃时，云璃能知道用户之前说了什么
        try:
            thread_tracker = get_thread_tracker()
            user_topic = plugin.persona_engine.language.detect_topic(message) if message else ""
            # 个人线程：保持一对一连续性
            thread_tracker.record_user_message(f"{group_id}:{user_id}", message, user_topic)
            # 群级线程：保持群内整体连续性（P2-11 修复）
            thread_tracker.record_user_message_to_group(group_id, user_id, user_nickname, message)
        except Exception:
            logger.debug("旁听模式：线程追踪失败", exc_info=True)

        # 2. 记录互动日志 + 群氛围 + 话题 + 轻量记忆提取（后台任务，不阻塞）
        #    _log_interaction 内部会调用：
        #    - db.log_interaction（记录到数据库）
        #    - update_atmosphere（通过回调，更新群氛围）
        #    - update_topic（通过回调，更新话题）
        #    - extract_memory_lightweight（轻量记忆提取）
        #    - update_topic_threads（话题线程追踪）
        plugin._safe_create_task(
            plugin._log_interaction(
                group_id, user_id, user_nickname, message, "", "passive",
            )
        )

        # 指标：旁听模式记录数
        plugin.metrics.increment("passive_records_total")

    async def _on_request_impl(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """on_request 的实际实现（被 measure() 包裹以统计耗时）"""
        plugin = self.plugin

        # Darwin 自动触发检查（不阻塞主流程）
        plugin._safe_create_task(
            plugin._evolution_manager._check_darwin_auto_trigger()
        )

        group_id = plugin._get_group_id(event)
        user_id = plugin._get_user_id(event)
        user_nickname = plugin._get_user_nickname(event)
        scope = f"req:{group_id}:{user_id}" if group_id or user_id else None
        # 结构化日志：绑定当前请求的 scope/user_id/group_id
        bind_context(
            scope=scope,
            user_id=user_id or None,
            group_id=group_id or None,
        )

        # 创建请求上下文，附着在 event 上
        ctx = RequestContext(
            req=req,
            group_id=group_id,
            user_id=user_id,
            user_nickname=user_nickname,
            scope=scope or (f"{group_id}:{user_id}" if group_id else user_id),
        )
        event._yunli_ctx = ctx

        # 消息防抖处理（由 MessageDebouncer 管理窗口和合并）
        if await plugin._debouncer.handle_message(ctx.scope, event, req):
            ctx.is_debounce_buffered = True
            # 指标：被防抖器合并
            plugin.metrics.increment("msg_processed_total", tag="debounce_merged")
            return

        # 指标：本次消息将走完整注入流程
        plugin.metrics.increment("msg_processed_total", tag="is_at_me")

        await self._inject_persona_prompt(event, req)
        # 处理成功后才记录时间戳，防止超时失败影响下一条消息的防抖判断
        plugin._debouncer.mark_processed(ctx.scope)

    async def _on_debounce_flush(
        self, scope: str, event: AstrMessageEvent, req: ProviderRequest
    ):
        """防抖窗口到期后，处理合并后的消息"""
        await self._inject_persona_prompt(event, req)

    async def _inject_persona_prompt(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """注入云璃人格提示词（带超时保护，防止慢查询阻塞LLM请求）"""
        try:
            await asyncio.wait_for(
                self._do_inject_persona_prompt(event, req),
                timeout=self.plugin._prompt_inject_timeout,
            )
            # 指标：注入成功
            self.plugin.metrics.increment("prompt_inject_total", tag="success")
        except asyncio.TimeoutError:
            logger.warning(
                "提示词注入超时(%ss)，跳过人格注入",
                self.plugin._prompt_inject_timeout,
            )
            # 指标：注入超时
            self.plugin.metrics.increment("prompt_inject_total", tag="timeout")
        except (OSError, RuntimeError, ValueError, AttributeError) as e:
            logger.exception("提示词注入失败: %s", e, extra={"phase": "request"})
            # 指标：注入失败
            self.plugin.metrics.increment("prompt_inject_total", tag="failure")

    def _batch_get_db_context(
        self, message: str, group_id: str, user_id: str, user_nickname: str
    ) -> tuple:
        """批量获取 DB 上下文（单线程内依次执行，减少 async 调度开销）

        Returns:
            (context_data, pending_loops, relationship_context)
        """
        plugin = self.plugin
        context_data = plugin.persona_engine.get_context_data(message, group_id, user_id)
        pending_loops = plugin.db.get_pending_loops(group_id, user_id, 2)
        relationship_context = plugin._context_builder.add_relationship_context(
            group_id, user_id, user_nickname, message, 150,
        )
        return context_data, pending_loops, relationship_context

    async def _do_inject_persona_prompt(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """注入云璃人格提示词（实际执行体）

        三阶段设计：
        Phase 1 — 纯内存运算（关系/情感/环境/场景）
        Phase 2 — 并行 DB 查询（get_context_data + get_pending_loops + add_relationship_context）
        Phase 3 — 组装提示词
        """
        plugin = self.plugin

        # 从 event 读取请求上下文
        ctx: RequestContext = getattr(event, "_yunli_ctx", None)
        if ctx is None:
            return  # 无上下文，跳过注入

        # 标记合并状态：防抖器将此消息与其他消息合并后 flush
        ctx.is_debounce_merged = ctx.is_debounce_buffered

        # 防止重复注入
        if ctx.is_prompt_injected:
            # 指标：dynamic context 缓存命中（跳过重新注入）
            plugin.metrics.increment("cache_hits")
            return
        # 指标：缓存未命中，需要重新注入
        plugin.metrics.increment("cache_misses")

        base_prompt = plugin.persona_engine.build_system_prompt()
        message = event.message_str or ""
        group_id = ctx.group_id
        user_id = ctx.user_id

        # ═══ Phase 1: 纯内存运算 ═══
        if group_id and user_id:
            plugin.relationship.update(group_id, user_id, message)
        if message:
            user_intent = plugin.relationship.detect_user_intent(message)
            emotion_trigger = plugin.relationship.INTENT_TO_EMOTION_TRIGGER.get(user_intent)
            if emotion_trigger:
                plugin.persona_engine.emotion.transition(emotion_trigger, message)

        # Phase 1 续：环境感知 + 场景信号 + 群聊氛围（全部纯内存，<1ms）
        env_context = plugin._context_builder.format_environment_perception()
        scene_desc = ""
        if plugin._enable_group_scene_perception and group_id:
            _GP = _gp()
            signals = _GP.extract_scene_signals(event)
            scene_desc = _GP.format_scene_description(signals)
        atmosphere_text = plugin._group_perception.get_atmosphere_text(group_id)
        user_nickname = ctx.user_nickname

        # Phase 1 续：知识查询模式检测
        ctx.is_knowledge_query = bool(
            message
            and plugin.persona_engine.language.detect_query_mode(message) == "knowledge_query"
        )

        if not ctx.is_knowledge_query:
            rel_mode = plugin.relationship.get_mode(group_id, user_id) if group_id and user_id else "normal"
            rel_hint = plugin.relationship.get_hint(group_id, user_id) if group_id and user_id else ""
            ctx.rel_mode = rel_mode
        else:
            rel_hint = ""
            ctx.rel_mode = "normal"

        # ═══ Phase 2: 批量 DB 查询 ═══
        db_result = await asyncio.to_thread(
            self._batch_get_db_context,
            message, group_id, user_id, user_nickname,
        )
        context_data, pending_loops, relationship_context = db_result

        # ═══ Phase 3: 组装 ═══
        # 3a. 静态 system_prompt
        if req.system_prompt:
            req.system_prompt = f"{req.system_prompt}\n\n[[YUNLI_BOUNDARY]]\n{base_prompt}"
        else:
            req.system_prompt = base_prompt

        # 3b. 动态上下文前缀
        dynamic_parts = []

        if env_context:
            dynamic_parts.append(env_context)
        if scene_desc:
            dynamic_parts.append(scene_desc)

        # 社交场景常识提示（极简，~10 Token）
        if message:
            social_scene = plugin._context_builder.detect_social_scene(message)
            if social_scene:
                dynamic_parts.append(f"【社交常识】{social_scene}")

        # 短期对话线程追踪（Phase 1 P0 改进）
        # 在动态上下文中注入"上一轮对话"信息，让 LLM 感知对话连续性
        if group_id and user_id and message:
            thread_tracker = get_thread_tracker()
            # 检测话题用于线程追踪
            user_topic = plugin.persona_engine.language.detect_topic(message) if message else ""

            # 个人线程：保持一对一连续性
            personal_scope = f"{group_id}:{user_id}"
            thread_tracker.record_user_message(personal_scope, message, user_topic)
            thread_context = thread_tracker.get_thread_context(personal_scope)
            if thread_context:
                dynamic_parts.append(f"【对话线程】{thread_context}")

            # 群级线程：保持群内整体连续性（P2-11 修复）
            # 让云璃和不同用户聊天时也能看到群里刚刚发生了什么
            thread_tracker.record_user_message_to_group(group_id, user_id, user_nickname, message)
            group_thread_context = thread_tracker.get_group_thread_context(group_id, max_lines=10)
            if group_thread_context:
                dynamic_parts.append(group_thread_context)

            # 注：线程状态（last_user_msg/last_yunli_resp/turn_count）由
            # ConversationThreadTracker 统一管理，不再同步到 RequestContext

        dynamic_prompt = plugin.persona_engine.build_dynamic_prompt(
            context_data, token_budget=300,
            relationship_context=relationship_context,
            rel_hint=rel_hint,
            user_nickname=user_nickname,
        )
        if dynamic_prompt:
            dynamic_parts.append(dynamic_prompt)

        chat_context = plugin._context_builder.build_chat_context(
            group_id, atmosphere_text, token_budget=200,
            group_perception=plugin._group_perception,
        )
        if chat_context:
            dynamic_parts.append(chat_context)

        # P0-2 / P2-11 修复：注入最近 20 条群聊原文，让云璃能看到群友具体说了什么
        # 解决原系统仅注入抽象话题/氛围，导致云璃无法接梗、关系错乱的问题
        recent_chat = plugin._context_builder.build_recent_chat_history(
            group_id, limit=20, exclude_message=message, token_budget=400,
        )
        if recent_chat:
            dynamic_parts.append(recent_chat)

        if pending_loops:
            loop_texts = []
            for loop in pending_loops:
                nickname = loop.get("user_nickname", "") or user_id[:4]
                loop_texts.append(f"{nickname}之前说{loop['text']}")
            if loop_texts:
                dynamic_parts.append(f"【待续】{'；'.join(loop_texts)}")

        # 注入动态上下文到用户消息（req.prompt）
        if dynamic_parts:
            dynamic_context = "\n\n".join(dynamic_parts)
            original_prompt = getattr(req, "prompt", None)
            if original_prompt is not None:
                req.prompt = f"[当前上下文]\n{dynamic_context}\n\n[用户消息]\n{original_prompt}"
            else:
                # 兼容旧版框架
                dynamic_full = f"{base_prompt}\n\n{dynamic_context}"
                if req.system_prompt:
                    req.system_prompt = f"{req.system_prompt}\n\n[[YUNLI_BOUNDARY]]\n{dynamic_full}"
                else:
                    req.system_prompt = dynamic_full

        ctx.is_prompt_injected = True

    # ========== LLM 响应后 ==========

    async def on_response(
        self, event: AstrMessageEvent, response: LLMResponse
    ):
        """@filter.on_llm_response 装饰的入口（实际执行体）

        由 YunliPersonaPlugin.on_llm_response 装饰器委派调用。
        """
        # 指标：on_response 总耗时
        with self.plugin.metrics.measure("on_response_duration"):
            await self._on_response_impl(event, response)

    async def _on_response_impl(
        self, event: AstrMessageEvent, response: LLMResponse
    ):
        """on_response 的实际实现（被 measure() 包裹以统计耗时）"""
        if not response or not response.completion_text:
            return

        # 从 event 读取请求上下文
        ctx: RequestContext = getattr(event, "_yunli_ctx", None)
        if not ctx or not ctx.is_prompt_injected:
            return

        # 结构化日志：绑定当前响应的 scope/user_id/group_id
        bind_context(
            scope=f"resp:{ctx.group_id}:{ctx.user_id}",
            user_id=ctx.user_id or None,
            group_id=ctx.group_id or None,
        )

        # 防抖合并中被跳过的消息
        if ctx.is_debounce_merged:
            response.completion_text = ""
            return

        plugin = self.plugin
        message = event.message_str or ""
        group_id = ctx.group_id
        user_id = ctx.user_id
        user_nickname = ctx.user_nickname

        # 检测是否为知识查询模式
        is_knowledge_query = ctx.is_knowledge_query
        if not is_knowledge_query:
            is_knowledge_query = is_structured_summary(response.completion_text or "")

        # 保存原始响应全文
        original_response = response.completion_text

        # 应用人格润色（第一段）
        rel_mode = getattr(ctx, "rel_mode", "normal")
        text = plugin.persona_engine.polish_response(
            original_response, message, is_first_segment=True, skip_emotion=True,
            relationship_mode=rel_mode,
        )

        # 回复自审（聊天模式：硬性短消息截断）
        chat_max_len = plugin.config.get("chat_max_text_length", 50)
        text = plugin.persona_engine.review_response(
            text, is_knowledge_query=is_knowledge_query, max_len=chat_max_len
        )

        # QQ 群聊特殊处理
        is_at_me = getattr(event, "is_at_me", lambda: False)()
        text = plugin.qq_behavior.format_for_qq(text, is_at_me, user_nickname, relationship_mode=rel_mode)

        # ========== 知识查询模式：完整输出，不切分 ==========
        if is_knowledge_query:
            max_len = plugin.config.get("knowledge_max_text_length", 4000)
            if len(text) > max_len:
                text = text[:max_len] + "…"
            response.completion_text = text
            self._log_token_usage(event, response)
            # 指标：每次 _log_interaction 触发都计为一次轻量级记忆提取
            # (extract_memory_lightweight 在 log_interaction 内部被调用)
            plugin.metrics.increment("memory_extractions_total")
            # P2-13 修复：await _log_interaction 确保日志落库后再返回，
            # 避免下一条消息来时还查不到云璃的回复
            await plugin._log_interaction(
                group_id, user_id, user_nickname, message, original_response, "llm"
            )
            # 立即刷新缓冲区，确保下一条消息能查到最新记录
            if hasattr(plugin.db, "flush_logs"):
                try:
                    plugin.db.flush_logs()
                except Exception:
                    pass
            return

        # ========== 聊天模式：后续处理 ==========

        # 关系模式回复长度限制
        if group_id and user_id:
            rel_length_limit = plugin.relationship.get_reply_length_limit(group_id, user_id)
            if rel_length_limit and len(text) > rel_length_limit:
                truncate_pos = rel_length_limit
                for i in range(rel_length_limit, max(rel_length_limit - 15, 0), -1):
                    if i < len(text) and text[i] in "。！？.!?…":
                        truncate_pos = i + 1
                        break
                text = text[:truncate_pos]

        # 拟人化处理
        if plugin.qq_behavior.should_skip_punctuation() and text.endswith("。"):
            text = text[:-1]
        emotion_state = (
            plugin.persona_engine.emotion.current_state
            if hasattr(plugin.persona_engine, "emotion")
            else "neutral"
        )
        text = plugin.qq_behavior.add_typing_pause(text, emotion_state=emotion_state, relationship_mode=rel_mode)
        text = plugin.qq_behavior.add_human_touches(text, emotion_state=emotion_state, relationship_mode=rel_mode)

        # 检查 AstrBot 是否启用了分段回复
        segmented_enabled = plugin._is_segmented_reply_enabled()

        if segmented_enabled and len(text) > plugin.message_splitter.max_segment_length:
            segments = plugin._prepare_segments(text)
            if segments:
                response.completion_text = segments[0]["text"]
                if len(segments) > 1:
                    try:
                        plugin._safe_create_task(
                            self._send_remaining_segments_with_sem(
                                event, segments[1:], group_id, user_id, user_nickname, message
                            )
                        )
                        # 指标：本次消息产生的分段数（含首段）
                        plugin.metrics.increment(
                            "segments_sent_total", value=len(segments)
                        )
                        # 指标：平均段数（histogram 简化版：直接存单次样本）
                        plugin.metrics.timing(
                            "segments_per_message", float(len(segments))
                        )
                    except (RuntimeError, asyncio.CancelledError) as e:
                        logger.exception("分段发送任务创建失败: %s", e, extra={"phase": "response"})
        else:
            response.completion_text = text

        # Token 使用监控
        self._log_token_usage(event, response)

        # 指标：每次 _log_interaction 触发都计为一次轻量级记忆提取
        # (extract_memory_lightweight 在 log_interaction 内部被调用)
        plugin.metrics.increment("memory_extractions_total")

        # 记录互动
        # P2-13 修复：await _log_interaction 确保日志落库后再返回，
        # 避免下一条消息来时还查不到云璃的回复
        await plugin._log_interaction(
            group_id, user_id, user_nickname, message, original_response,
            "llm", response_filtered=text,
        )

        # 短期对话线程追踪：记录云璃回复（Phase 1 P0 改进）
        if group_id and user_id:
            try:
                thread_tracker = get_thread_tracker()
                # 个人线程：保持一对一连续性
                thread_tracker.record_yunli_response(f"{group_id}:{user_id}", text)
                # 群级线程：保持群内整体连续性（P2-11 修复）
                thread_tracker.record_yunli_response_to_group(group_id, text)
            except (RuntimeError, AttributeError):
                pass  # 线程追踪失败不影响主流程

        # P2-13 修复：立即刷新日志缓冲区，确保下一条消息能查到最新记录
        # 必须在 await _log_interaction 之后调用，否则可能刷不到刚写入的缓冲数据
        if hasattr(plugin.db, "flush_logs"):
            try:
                plugin.db.flush_logs()
            except Exception:
                pass

    async def _send_remaining_segments_with_sem(
        self,
        event: AstrMessageEvent,
        segments: List[Dict],
        group_id: str,
        user_id: str,
        user_nickname: str,
        original_message: str,
    ):
        """发送剩余的片段（带信号量限制并发数）"""
        async with self.plugin._segment_send_sem:
            await self._send_remaining_segments(
                event, segments, group_id, user_id, user_nickname, original_message
            )

    async def _send_remaining_segments(
        self,
        event: AstrMessageEvent,
        segments: List[Dict],
        group_id: str,
        user_id: str,
        user_nickname: str,
        original_message: str,
    ):
        """发送剩余的片段（模拟真人分段打字）

        后续片段不再添加语气词，避免碎嘴。
        segments 已由 _prepare_segments 预处理（含空段过滤和思考停顿）。
        """
        plugin = self.plugin

        # 从请求上下文获取关系模式（修复 rel_mode 未定义 bug）
        # rel_mode 原仅在 _on_response_impl 作用域内定义，此处需从 ctx 读取
        ctx = getattr(event, "_yunli_ctx", None)
        rel_mode = getattr(ctx, "rel_mode", "normal") if ctx else "normal"

        # 检查事件是否仍然有效
        try:
            _ = getattr(event, "message_str", None)
        except (AttributeError, RuntimeError):
            # 类别 A：已知可恢复，事件已失效，debug 级别即可
            logger.debug("分段发送中止：事件已失效")
            return

        total_timeout = 30.0
        start_time = time.time()

        for seg_info in segments:
            if time.time() - start_time >= total_timeout:
                logger.warning(
                    "分段发送超时(%ss)，终止剩余 %d 段", total_timeout, len(segments)
                )
                break

            delay = seg_info.get("delay", 0.5)
            text = seg_info["text"]

            # 后续片段不再添加语气词
            text = plugin.persona_engine.polish_response(
                text, original_message, is_first_segment=False, relationship_mode=rel_mode
            )

            if delay > 0:
                await asyncio.sleep(min(delay, total_timeout - (time.time() - start_time)))

            if text.strip():
                try:
                    await event.send(MessageChain([Plain(text)]))
                except (RuntimeError, ConnectionError, OSError) as e:
                    logger.exception("分段发送失败: %s", e, extra={"phase": "response"})
                    break

    def _log_token_usage(self, event: AstrMessageEvent, response: LLMResponse):
        """记录 Token 使用情况（采样记录）"""
        plugin = self.plugin
        try:
            # 指标：每次响应都计为一次 LLM 调用
            plugin.metrics.increment("llm_call_total")

            interval = max(1, int(1 / max(plugin._token_log_sample_rate, 0.01)))
            plugin._token_log_counter += 1
            should_log = (plugin._token_log_counter % interval == 0)
            warn_interval = max(1, interval // 5)

            system_prompt = ""
            if hasattr(response, "system_prompt") and response.system_prompt:
                system_prompt = response.system_prompt
            elif hasattr(event, "system_prompt") and event.system_prompt:
                system_prompt = event.system_prompt

            system_tokens = estimate_tokens(system_prompt) if system_prompt else 0
            response_tokens = (
                estimate_tokens(response.completion_text) if response.completion_text else 0
            )
            total_tokens = system_tokens + response_tokens

            # 指标：LLM 调用的"Token 数量"采样（用 timing 容器存大小样本，
            # 保留最近 100 次样本，便于观察趋势）
            plugin.metrics.timing("llm_call_duration_ms", float(total_tokens))

            # 高 Token 警告
            if system_tokens > 1200 and (plugin._token_log_counter % warn_interval == 0):
                logger.warning(
                    "Token使用过高：系统提示词 %d Token，建议优化",
                    system_tokens,
                    extra={"sample": 1, "tokens": system_tokens},
                )

            if should_log:
                logger.debug(
                    "Token使用：系统 %d / 响应 %d / 合计 %d",
                    system_tokens, response_tokens,
                    total_tokens,
                    extra={
                        "sample": 1,
                        "tokens": total_tokens,
                    },
                )

        except (ValueError, TypeError, AttributeError):
            logger.debug("Token监控异常", exc_info=True)
