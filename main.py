import re
import time
import random
import asyncio
import logging
from pathlib import Path
from typing import List, Dict, Optional, Set

from astrbot.api.star import Context, Star, register
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest, LLMResponse
from astrbot.api.message_components import At, Plain
from astrbot.core.message.message_event_result import MessageChain

from .database import YunliDatabase
from .persona import YunliPersonaEngine, QQBehaviorManager, RelationshipManager, MessageSplitter
from .core import (
    MessageDebouncer,
    ContextBuilder,
    GroupPerception,
    MemoryManager,
    AtDetector,
    RequestContext,
    estimate_tokens,
    truncate_at_sentence,
    is_structured_summary,
)


@register(
    "astrbot_plugin_yunli_persona",
    "YunliDev",
    "云璃QQ群聊人格插件 - 让云璃成为你的群友",
    "1.6.0",
    "https://github.com/yourname/astrbot_plugin_yunli_persona",
)
class YunliPersonaPlugin(Star):
    """云璃人格插件主类"""

    logger = logging.getLogger("YunliPlugin")

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

        # 初始化数据库
        data_dir = (
            Path(context.data_dir)
            if hasattr(context, "data_dir")
            else Path(__file__).parent / "data"
        )
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = data_dir / "yunli_database.db"
        self.db = YunliDatabase(str(db_path))
        self._init_data()

        # 初始化人格引擎
        self.persona_engine = YunliPersonaEngine(self.db, self.config)

        # 初始化QQ群聊行为管理器
        self.qq_behavior = QQBehaviorManager(self.db, self.config)

        # 初始化关系状态机
        relationship_config = {
            "relationship_decay_multiplier": self.config.get("relationship_decay_multiplier", 1.0),
        }
        self.relationship = RelationshipManager(relationship_config)
        self.qq_behavior._relationship_manager = self.relationship

        # 初始化消息切分器
        splitter_config = {
            'max_segment_length': self.config.get('message_splitter_max_segment_length', 180),
            'min_segment_length': self.config.get('message_splitter_min_segment_length', 10),
            'enable_typing_delay': self.config.get('message_splitter_enable_typing_delay', True),
            'base_delay': self.config.get('message_splitter_base_delay', 0.5),
            'delay_per_char': self.config.get('message_splitter_delay_per_char', 0.03),
            'max_delay': self.config.get('message_splitter_max_delay', 3.0),
            'enable_thinking_pause': self.config.get('message_splitter_enable_thinking_pause', True),
            'thinking_pause_prob': self.config.get('message_splitter_thinking_pause_prob', 0.3),
            'enable_natural_break': self.config.get('message_splitter_enable_natural_break', True),
        }
        self.message_splitter = MessageSplitter(splitter_config)

        # 消息防抖器（独立模块）
        self._debouncer = MessageDebouncer(
            debounce_seconds=self.config.get("message_debounce_seconds", 3.0),
            max_wait_seconds=self.config.get("message_debounce_max_wait", 8.0),
            on_flush=self._on_debounce_flush,
        )

        # At 检测器（可缓存 self_id，测试可替换）
        self._at_detector = AtDetector()

        # 上下文构建器（环境感知 + 关系上下文 + 群聊上下文）
        self._context_builder = ContextBuilder(self.db, self.persona_engine, self.relationship, self.config)

        # 群聊感知器（话题检测 + 群氛围 + 场景信号）
        self._group_perception = GroupPerception(self.db, self.config)

        # 记忆管理器（轻量提取 + LLM深度整理）
        self._memory_manager = MemoryManager(self.db, self.config, context)

        # 超时控制：阻止慢查询阻塞LLM请求
        self._prompt_inject_timeout = self.config.get("prompt_inject_timeout_seconds", 10)

        # 配置快捷引用
        self._enable_group_scene_perception = self.config.get("enable_group_scene_perception", True)
        self._enable_high_intensity = self.config.get("enable_high_intensity", True)
        self._high_intensity_window = self.config.get("high_intensity_window_seconds", 60)
        self._high_intensity_threshold = self.config.get("high_intensity_threshold", 3)
        self._high_intensity_cooldown = self.config.get("high_intensity_cooldown_seconds", 150)
        self._enable_topic_threads = self.config.get("enable_topic_threads", True)
        self._max_topic_threads = self.config.get("max_topic_threads", 8)
        self._topic_thread_ttl_minutes = self.config.get("topic_thread_ttl_minutes", 90)

        # 分段发送信号量
        self._segment_send_sem = asyncio.Semaphore(3)

        # Token监控采样率（只记录 1/N 的请求，减少日志噪音）
        self._token_log_sample_rate = self.config.get("token_log_sample_rate", 0.1)
        self._token_log_counter = 0

        # 缓存 self_id，避免每次 _should_activate 都重复查找
        self._cached_self_id: Optional[str] = None

        # 后台任务生命周期管理：保存所有 create_task 的引用，支持插件卸载时优雅清理
        self._background_tasks: Set[asyncio.Task] = set()

    def _safe_create_task(self, coro) -> asyncio.Task:
        """创建后台任务并跟踪生命周期

        所有 asyncio.create_task 的替代入口，确保：
        1. 任务失败时自动从集合移除并有日志
        2. 插件卸载（__del__）时可取消所有未完成任务
        """
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)

        def _done_callback(t: asyncio.Task):
            self._background_tasks.discard(t)
            if not t.cancelled() and t.exception():
                print(f"[云璃插件] 后台任务异常: {t.exception()}")

        task.add_done_callback(_done_callback)
        return task

    def _init_data(self):
        """初始化数据库数据"""
        # 检查是否已有数据
        dialogues = self.db.query_dialogues("greeting", limit=1)
        if not dialogues:
            # 导入初始数据（从插件目录读取静态数据文件）
            data_path = (
                Path(__file__).parent / "database" / "data" / "initial_data.json"
            )
            if data_path.exists():
                self.db.import_from_json(str(data_path))
                print("[云璃插件] 初始数据导入完成")
            else:
                print(f"[云璃插件] 警告：未找到初始数据文件 {data_path}")

    @filter.on_llm_request(priority=50)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """在LLM请求前注入云璃人格提示词

        Token预算：
        - 基础提示词: ~500 Token
        - 动态知识: 最多300 Token
        - 关系上下文: 最多150 Token
        - 群聊上下文: 最多200 Token
        - 总计: ~1150 Token

        消息防抖：同一scope（群号+用户ID）在窗口内的多条消息合并处理。
        """
        if not self._should_activate(event):
            return

        group_id = self._get_group_id(event)
        user_id = self._get_user_id(event)
        user_nickname = self._get_user_nickname(event)
        scope = f"{group_id}:{user_id}" if group_id else user_id

        # 创建请求上下文，附着在 event 上
        ctx = RequestContext(
            req=req,
            group_id=group_id,
            user_id=user_id,
            user_nickname=user_nickname,
            scope=scope,
        )
        event._yunli_ctx = ctx

        # 消息防抖处理（由 MessageDebouncer 管理窗口和合并）
        if await self._debouncer.handle_message(scope, event, req):
            ctx.is_debounce_buffered = True
            return

        await self._inject_persona_prompt(event, req)
        # 处理成功后才记录时间戳，防止超时失败影响下一条消息的防抖判断
        self._debouncer.mark_processed(scope)

    async def _on_debounce_flush(self, scope: str, event: AstrMessageEvent, req: ProviderRequest):
        """防抖窗口到期后，处理合并后的消息"""
        await self._inject_persona_prompt(event, req)

    async def _inject_persona_prompt(self, event: AstrMessageEvent, req: ProviderRequest):
        """注入云璃人格提示词（带超时保护，防止慢查询阻塞LLM请求）"""
        try:
            await asyncio.wait_for(
                self._do_inject_persona_prompt(event, req),
                timeout=self._prompt_inject_timeout,
            )
        except asyncio.TimeoutError:
            print(
                f"[云璃插件] 提示词注入超时({self._prompt_inject_timeout}s)，"
                f"跳过人格注入"
            )
        except Exception as e:
            print(f"[云璃插件] 提示词注入失败: {e}")

    def _batch_get_db_context(self, message: str, group_id: str, user_id: str, user_nickname: str) -> tuple:
        """批量获取 DB 上下文（单线程内依次执行，减少 async 调度开销）

        Returns:
            (context_data, pending_loops, relationship_context)
        """
        context_data = self.persona_engine.get_context_data(message, group_id, user_id)
        pending_loops = self.db.get_pending_loops(group_id, user_id, 2)
        relationship_context = self._context_builder.add_relationship_context(
            group_id, user_id, user_nickname, message, 150,
        )
        return context_data, pending_loops, relationship_context

    async def _do_inject_persona_prompt(self, event: AstrMessageEvent, req: ProviderRequest):
        """注入云璃人格提示词（实际执行体）

        三阶段设计：
        Phase 1 — 纯内存运算（关系/情感/环境/场景）
        Phase 2 — 并行 DB 查询（get_context_data + get_pending_loops + add_relationship_context）
        Phase 3 — 组装提示词
        """
        # 从 event 读取请求上下文
        ctx: RequestContext = getattr(event, '_yunli_ctx', None)
        if ctx is None:
            return  # 无上下文，跳过注入

        # 标记合并状态：防抖器将此消息与其他消息合并后 flush
        ctx.is_debounce_merged = ctx.is_debounce_buffered

        # 防止重复注入：如果 req.system_prompt 已有内容（来自框架或其他插件），
        # 检查是否已经包含云璃标记。如果没有，追加而非覆盖。
        if ctx.is_prompt_injected:
            return

        base_prompt = self.persona_engine.build_system_prompt()
        message = event.message_str or ""
        group_id = ctx.group_id
        user_id = ctx.user_id

        # ═══ Phase 1: 纯内存运算 ═══
        if group_id and user_id:
            self.relationship.update(group_id, user_id, message)
        if message:
            user_intent = self.relationship.detect_user_intent(message)
            emotion_trigger = self.relationship.INTENT_TO_EMOTION_TRIGGER.get(user_intent)
            if emotion_trigger:
                self.persona_engine.emotion.transition(emotion_trigger, message)

        # Phase 1 续：环境感知 + 场景信号 + 群聊氛围（全部纯内存，<1ms）
        env_context = self._context_builder.format_environment_perception()
        scene_desc = ""
        if self._enable_group_scene_perception and group_id:
            signals = GroupPerception.extract_scene_signals(event)
            scene_desc = GroupPerception.format_scene_description(signals)
        atmosphere_text = self._group_perception.get_atmosphere_text(group_id)
        user_nickname = ctx.user_nickname

        # Phase 1 续：知识查询模式检测（基于用户消息，避免响应阶段重复计算）
        ctx.is_knowledge_query = bool(
            message
            and self.persona_engine.language.detect_query_mode(message) == "knowledge_query"
        )

        # ═══ Phase 2: 批量 DB 查询（单次 to_thread，减少线程池争用） ═══
        # 将三个独立查询（知识库 + 约定 + 关系上下文）打包到一次线程调用中执行
        db_result = await asyncio.to_thread(
            self._batch_get_db_context,
            message, group_id, user_id, user_nickname,
        )
        context_data, pending_loops, relationship_context = db_result

        # ═══ Phase 3: 组装提示词（全部纯内存） ═══
        prompt_parts = [base_prompt]

        # 3a. 环境感知 + 场景信号
        if env_context:
            prompt_parts.append(env_context)
        if scene_desc:
            prompt_parts.append(scene_desc)

        # 3b. 动态知识
        dynamic_prompt = self.persona_engine.build_dynamic_prompt(
            context_data, token_budget=300
        )
        if dynamic_prompt:
            prompt_parts.append(dynamic_prompt)

        # 3c. 关系状态提示
        if group_id and user_id:
            rel_hint = self.relationship.get_hint(group_id, user_id)
            if rel_hint:
                prompt_parts.append(f"【注意】{rel_hint}")

        # 3d. 关系上下文
        if relationship_context:
            prompt_parts.append(relationship_context)

        # 3e. 群聊上下文
        chat_context = self._context_builder.build_chat_context(
            group_id, atmosphere_text, token_budget=200
        )
        if chat_context:
            prompt_parts.append(chat_context)

        # 3f. 未完成约定
        if pending_loops:
            loop_texts = []
            for loop in pending_loops:
                nickname = loop.get("user_nickname", "") or user_id[:4]
                loop_texts.append(f"{nickname}之前说{loop['text']}")
            if loop_texts:
                prompt_parts.append(f"【待续】{'；'.join(loop_texts)}")

        full_prompt = "\n\n".join(prompt_parts)

        # 注入提示词（处理与其他插件共存的场景）
        if req.system_prompt:
            # 其他插件已注入提示词 → 在云璃标记之后追加
            # 使用 [[YUNLI_BOUNDARY]] 标记作为分隔，方便后续追踪
            req.system_prompt = f"{req.system_prompt}\n\n[[YUNLI_BOUNDARY]]\n{full_prompt}"
        else:
            req.system_prompt = full_prompt

        ctx.is_prompt_injected = True

    @filter.on_llm_response(priority=50)
    async def on_llm_response(self, event: AstrMessageEvent, response: LLMResponse):
        """后处理LLM响应（含Token使用监控与分段发送）"""
        if not response or not response.completion_text:
            return

        # 从 event 读取请求上下文
        ctx: RequestContext = getattr(event, '_yunli_ctx', None)
        if not ctx or not ctx.is_prompt_injected:
            # 未注入云璃提示词 → 跳过云璃的后处理
            # 注意：不能清空 response.completion_text，其他插件可能已注入自己的提示词并产生了响应
            return

        # 检查是否是防抖合并中被跳过的消息
        if ctx.is_debounce_merged:
            response.completion_text = ""
            return

        message = event.message_str or ""
        group_id = ctx.group_id
        user_id = ctx.user_id
        user_nickname = ctx.user_nickname

        # 检测是否为知识查询模式
        is_knowledge_query = ctx.is_knowledge_query
        if not is_knowledge_query:
            is_knowledge_query = is_structured_summary(response.completion_text or "")

        # 保存原始响应全文（用于日志记录，避免记录截断/润色后的文本）
        original_response = response.completion_text

        # 应用人格润色（第一段，允许加语气词）
        # skip_emotion=True: Phase 1 已完成情绪检测，不重复触发
        text = self.persona_engine.polish_response(
            original_response, message, is_first_segment=True, skip_emotion=True,
        )

        # 回复自审（检测助手腔/泄露内部状态/过长/重复标点）
        text = self.persona_engine.review_response(text, is_knowledge_query=is_knowledge_query)

        # QQ群聊特殊处理（知识查询模式下减少拟人化，保持内容清晰）
        is_at_me = getattr(event, 'is_at_me', lambda: False)()
        text = self.qq_behavior.format_for_qq(text, is_at_me, user_nickname)

        # ========== 知识查询模式：完整输出，不切分（早退，跳过聊天模式所有处理） ==========
        if is_knowledge_query:
            max_len = self.config.get("knowledge_max_text_length", 4000)
            if len(text) > max_len:
                text = text[:max_len] + "…"
            response.completion_text = text
            self._log_token_usage(event, response)
            self._safe_create_task(
                self._log_interaction(group_id, user_id, user_nickname, message, original_response, "llm")
            )
            return

        # ========== 聊天模式：后续处理 ==========

        # 关系模式回复长度限制
        if group_id and user_id:
            rel_length_limit = self.relationship.get_reply_length_limit(group_id, user_id)
            if rel_length_limit and len(text) > rel_length_limit:
                truncate_pos = rel_length_limit
                for i in range(rel_length_limit, max(rel_length_limit - 15, 0), -1):
                    if i < len(text) and text[i] in '。！？.!?…':
                        truncate_pos = i + 1
                        break
                text = text[:truncate_pos]

        # 拟人化处理
        if self.qq_behavior.should_skip_punctuation() and text.endswith("。"):
            text = text[:-1]
        text = self.qq_behavior.add_typing_pause(text)
        text = self.qq_behavior.add_human_touches(text)

        # 检查 AstrBot 是否启用了分段回复
        segmented_enabled = self._is_segmented_reply_enabled()

        if segmented_enabled and len(text) > self.message_splitter.max_segment_length:
            segments = self._prepare_segments(text)
            if segments:
                response.completion_text = segments[0]['text']
                if len(segments) > 1:
                    try:
                        self._safe_create_task(
                            self._send_remaining_segments_with_sem(
                                event, segments[1:], group_id, user_id, user_nickname, message
                            )
                        )
                    except Exception as e:
                        print(f"[云璃插件] 分段发送任务创建失败: {e}")
        else:
            response.completion_text = text

        # Token使用监控
        self._log_token_usage(event, response)

        # 记录互动（使用原始响应全文，不记录截断/润色后的文本）
        self._safe_create_task(
            self._log_interaction(group_id, user_id, user_nickname, message, original_response, "llm")
        )

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
        async with self._segment_send_sem:
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

        注意：此函数通过 asyncio.create_task 被调用，内部不能直接使用 yield。
        使用 event.send() 直接发送消息。
        后续片段不再添加语气词，避免碎嘴。

        segments 已由 _prepare_segments 预处理（含空段过滤和思考停顿）。
        """
        # 检查事件是否仍然有效（插件卸载时事件可能已失效）
        try:
            _ = getattr(event, 'message_str', None)
        except Exception:
            print(f"[云璃插件] 分段发送中止：事件已失效")
            return

        total_timeout = 30.0
        start_time = time.time()

        for seg_info in segments:
            if time.time() - start_time >= total_timeout:
                print(f"[云璃插件] 分段发送超时({total_timeout}s)，终止剩余 {len(segments)} 段")
                break

            delay = seg_info.get('delay', 0.5)
            text = seg_info['text']

            # 后续片段不再添加语气词（is_first_segment=False）
            text = self.persona_engine.polish_response(text, original_message, is_first_segment=False)

            if delay > 0:
                await asyncio.sleep(min(delay, total_timeout - (time.time() - start_time)))

            if text.strip():
                try:
                    await event.send(text)
                except Exception as e:
                    print(f"[云璃插件] 分段发送失败: {e}")
                    break

    def _is_segmented_reply_enabled(self) -> bool:
        """检查 AstrBot 是否启用了分段回复"""
        try:
            # 从 AstrBot 配置中读取分段回复设置
            if hasattr(self.context, 'config') and self.context.config:
                platform_settings = getattr(self.context.config, 'platform_settings', None)
                if platform_settings:
                    segmented = platform_settings.get('segmented_reply', {})
                    return segmented.get('enable', False)
        except Exception:
            pass

        # 默认启用插件自身的分段逻辑（如果用户配置了的话）
        return self.config.get('force_segmented_reply', True)

    def _log_token_usage(self, event: AstrMessageEvent, response: LLMResponse):
        """记录Token使用情况（采样记录）"""
        try:
            # 采样间隔（如 sample_rate=0.1 则每 10 条记录一次）
            interval = max(1, int(1 / max(self._token_log_sample_rate, 0.01)))
            self._token_log_counter += 1
            should_log = (self._token_log_counter % interval == 0)
            warn_interval = max(1, interval // 5)  # 高Token警告更频繁

            system_prompt = ""
            if hasattr(response, "system_prompt") and response.system_prompt:
                system_prompt = response.system_prompt
            elif hasattr(event, "system_prompt") and event.system_prompt:
                system_prompt = event.system_prompt

            system_tokens = len(system_prompt) // 2 if system_prompt else 0
            response_tokens = (
                len(response.completion_text) // 2 if response.completion_text else 0
            )

            # 高Token警告（不受采样率影响，但限制重复频率）
            if system_tokens > 1200 and (self._token_log_counter % warn_interval == 0):
                self.logger.warning(
                    "Token使用过高：系统提示词 %d Token，建议优化",
                    system_tokens,
                )

            if should_log:
                self.logger.debug(
                    "Token使用：系统 %d / 响应 %d / 合计 %d",
                    system_tokens, response_tokens,
                    system_tokens + response_tokens,
                )

        except Exception as e:
            self.logger.debug("Token监控异常: %s", e)

    @filter.command("云璃")
    async def cmd_yunli(self, event: AstrMessageEvent):
        """云璃命令 - 获取云璃的回应（含基本上下文感知）"""
        message = event.message_str or ""
        # 去除命令前缀
        message = message.replace("/云璃", "").replace("云璃", "", 1).strip()
        group_id = self._get_group_id(event)
        user_id = self._get_user_id(event)

        # 注入基本上下文（关系 + 记忆，不阻塞主流程）
        context_meta = ""
        if group_id and user_id:
            try:
                rel_hint = self.relationship.get_hint(group_id, user_id)
                if rel_hint:
                    context_meta += f" ({rel_hint.split('，')[0]})"
            except Exception:
                pass

        if not message:
            # 没有参数，返回随机台词
            response = self.persona_engine.get_direct_response("你好" + context_meta)
            if not response:
                response = "嗯？叫我有什么事吗？"
        else:
            # 尝试获取直接响应
            response = self.persona_engine.get_direct_response(message + context_meta)
            if not response:
                response = f"{message}？这是什么意思？"

        # 润色
        response = self.persona_engine.polish_response(response, message)
        response = self.qq_behavior.format_for_qq(response)

        # 命令响应统一走分段发送
        async for result in self._send_segmented(event, response):
            yield result

    @filter.command("云璃语音")
    async def cmd_voice(self, event: AstrMessageEvent):
        """获取云璃的语音台词"""
        message = event.message_str or ""
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

        voice_line = self.persona_engine.get_voice_line(line_type)
        if voice_line:
            response = voice_line
        else:
            response = "哼，现在不想说话。"

        yield event.plain_result(response)

    @filter.command("云璃资料")
    async def cmd_knowledge(self, event: AstrMessageEvent):
        """查询云璃相关知识"""
        message = event.message_str or ""
        keyword = message.replace("/云璃资料", "").replace("云璃资料", "", 1).strip()

        if not keyword:
            yield event.plain_result("你要查什么？跟我说说看。")
            return

        # 查询知识库
        knowledge = self.db.query_knowledge(keyword, limit=3)

        if knowledge:
            responses = []
            for k in knowledge:
                responses.append(f"【{k['entity_name']}】{k['description']}")
            response = "\n".join(responses)
        else:
            response = f"关于'{keyword}'…我也不太清楚呢。你要不要教教我？"

        # 知识查询结果统一走分段发送
        async for result in self._send_segmented(event, response):
            yield result

    @filter.command("云璃帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """显示帮助信息"""
        help_text = """【云璃插件帮助】
/云璃 [内容] - 跟云璃说话
/云璃语音 [类型] - 听云璃的台词（战斗/技能/胜利/待机）
/云璃资料 [关键词] - 查询相关知识
/云璃帮助 - 显示本帮助

也可以直接@我，我会回应你的~"""

        # 帮助文本较短，直接发送
        yield event.plain_result(help_text)

    def _should_activate(self, event: AstrMessageEvent) -> bool:
        """判断是否激活云璃人格

        委托给 AtDetector 进行多层 @ 检测（框架方法 → 消息组件 → 文本匹配）。
        self_id 带缓存，首次查找后将结果注入 AtDetector。
        """
        return self._at_detector.is_at_me(event, override_self_id=self._get_cached_self_id())

    def _get_cached_self_id(self) -> str:
        """获取机器人自身QQ号（带缓存），同时注入 AtDetector"""
        if self._cached_self_id is not None:
            return self._cached_self_id
        try:
            if hasattr(self.context, "get_self_id"):
                self._cached_self_id = str(self.context.get_self_id())
                self._at_detector.set_self_id(self._cached_self_id)
                return self._cached_self_id
            platform = getattr(self.context, "platform", None)
            if platform:
                account = getattr(platform, "account", None)
                if account:
                    self._cached_self_id = str(getattr(account, "u", "") or "")
                    self._at_detector.set_self_id(self._cached_self_id)
                    return self._cached_self_id
        except Exception:
            pass
        self._cached_self_id = ""
        return ""

    def _get_self_id(self) -> str:
        """获取机器人自身QQ号（委托缓存版本）"""
        return self._get_cached_self_id()

    # ========== 交互日志 ==========

    def _log_interaction(self, group_id, user_id, user_nickname, message, response, trigger_type):
        """记录交互日志到记忆系统"""
        emotion_state = self.persona_engine.emotion.current_state if hasattr(self.persona_engine, 'emotion') else ""
        self._memory_manager.log_interaction(
            group_id, user_id, user_nickname, message, response, trigger_type,
            emotion_state=emotion_state,
            on_atmosphere_update=self._group_perception.update_atmosphere,
            on_topic_update=self._group_perception.detect_topic,
        )

        # 话题线程追踪（不阻塞主流程）
        if self._enable_topic_threads and group_id and user_id:
            self._group_perception.update_topic_threads(
                group_id, user_id, user_nickname, message,
                self._topic_thread_ttl_minutes, self._max_topic_threads,
            )

    # ========== 消息发送 ==========

    def _prepare_segments(self, text: str) -> List[Dict]:
        """将文本切分为段（简化版）

        不再做空段过滤和 Markdown 检测，交由简化后的 MessageSplitter 处理。
        """
        segmented_enabled = self._is_segmented_reply_enabled()
        if not segmented_enabled or len(text) <= self.message_splitter.max_segment_length:
            return [{"text": text, "delay": 0}]

        raw_segments = self.message_splitter.split(text)
        if not raw_segments:
            return [{"text": text, "delay": 0}]

        result = []
        for i, seg_info in enumerate(raw_segments):
            seg_text = seg_info['text'].strip()
            if not seg_text:
                continue

            # 思考停顿（仅在非首段）
            if i > 0 and self.message_splitter.enable_thinking_pause:
                pause = self.message_splitter.get_thinking_pause(
                    seg_text=seg_text, is_first=False
                )
                if pause:
                    seg_text = seg_text + pause

            result.append({
                "text": seg_text,
                "delay": seg_info.get('delay', 0.3),
            })

        return result if result else [{"text": text, "delay": 0}]

    async def _send_segmented(self, event: AstrMessageEvent, text: str):
        """统一分段发送消息（命令路径，使用 yield event.plain_result()）"""
        if not text or not text.strip():
            return

        segments = self._prepare_segments(text)
        total_timeout = 30.0
        start_time = time.time()

        for i, seg_info in enumerate(segments):
            if time.time() - start_time >= total_timeout:
                break
            seg_text = seg_info['text']
            delay = seg_info.get('delay', 0)

            if i > 0 and delay > 0:
                await asyncio.sleep(min(delay, total_timeout - (time.time() - start_time)))

            if seg_text.strip():
                yield event.plain_result(seg_text)

    def _get_group_id(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_group_id())
        except:
            return ""

    def _get_user_id(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id())
        except:
            return ""

    def _get_user_nickname(self, event: AstrMessageEvent) -> str:
        try:
            return event.get_sender_name() or ""
        except:
            return ""

    def __del__(self):
        """清理资源：取消后台任务、刷新DB日志缓冲区、关闭连接"""
        # 1. 取消所有未完成的后台任务
        for task in list(self._background_tasks):
            if not task.done():
                task.cancel()
        self._background_tasks.clear()

        # 2. 刷新DB日志缓冲区
        if hasattr(self, "db") and hasattr(self.db, "flush_logs"):
            try:
                self.db.flush_logs()
            except Exception:
                pass

        # 3. 清空防抖缓冲区
        if hasattr(self, "_debouncer"):
            self._debouncer.clear()

        # 4. 关闭数据库连接
        if hasattr(self, "db"):
            try:
                self.db.close()
            except Exception:
                pass
