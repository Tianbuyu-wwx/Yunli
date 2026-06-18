"""云璃人格插件主入口（已重构）

AstrBot 插件入口（@register），通过装饰器接收 AstrBot 框架事件，
并把所有重业务委派给三个协作者类：

  - YunliEventPipeline    LLM 请求前/响应后的事件管线
  - YunliCommandHandler   /云璃 / /云璃语音 / /云璃资料 / /云璃帮助
  - YunliEvolutionManager /云璃进化（Darwin 进化 + Phase 2 模式发现）

本类仅保留 @register 装饰、初始化、共享 state、生命周期清理与装饰器
委托方法（保持向后兼容：所有 self.plugin.xxx 接口对测试和框架可见）。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

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
)
from .core.thread_tracker import get_thread_tracker
from .evolution.darwin_evolve import DarwinEvolution
from .evolution.log_collector import LogCollector, InteractionLog
from .evolution.pattern_discovery import PatternDiscovery
from .evolution.rule_generator import RuleGenerator

from .core.event_pipeline import YunliEventPipeline
from .core.command_handler import YunliCommandHandler
from .core.evolution_manager import YunliEvolutionManager
from .core.metrics import Metrics


@register(
    "astrbot_plugin_yunli_persona",
    "YunliDev",
    "云璃QQ群聊人格插件 - 让云璃成为你的群友",
    "2.3.1",
    "https://github.com/YunliDev/astrbot_plugin_yunli_persona",
)
class YunliPersonaPlugin(Star):
    """云璃人格插件主类（@register 装饰）

    保持向后兼容：所有原 self.plugin.xxx 接口仍可被测试/框架访问。
    重业务逻辑已迁出至 YunliEventPipeline / YunliCommandHandler /
    YunliEvolutionManager 三个协作者类。
    """

    logger = logging.getLogger(__name__)

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

        # 初始化消息切分器（短消息策略：最多2段，每段80字）
        splitter_config = {
            'max_segment_length': self.config.get('message_splitter_max_segment_length', 80),
            'min_segment_length': self.config.get('message_splitter_min_segment_length', 10),
            'enable_typing_delay': self.config.get('message_splitter_enable_typing_delay', True),
            'base_delay': self.config.get('message_splitter_base_delay', 1.5),
            'delay_per_char': self.config.get('message_splitter_delay_per_char', 0.04),
            'max_delay': self.config.get('message_splitter_max_delay', 4.0),
            'enable_thinking_pause': self.config.get('message_splitter_enable_thinking_pause', True),
            'thinking_pause_prob': self.config.get('message_splitter_thinking_pause_prob', 0.3),
            'enable_natural_break': self.config.get('message_splitter_enable_natural_break', True),
            'max_segments': self.config.get('message_splitter_max_segments', 2),
        }
        self.message_splitter = MessageSplitter(splitter_config)

        # 消息防抖器（独立模块）—— 回调需要 plugin 实例的 _on_debounce_flush
        # 现在通过 self._event_pipeline._on_debounce_flush 转发（plugin 仍提供同名薄方法）
        self._debouncer = MessageDebouncer(
            debounce_seconds=self.config.get("message_debounce_seconds", 3.0),
            max_wait_seconds=self.config.get("message_debounce_max_wait", 8.0),
            on_flush=self._on_debounce_flush,
            on_individual_message=self._on_individual_message,
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

        # Darwin 进化系统（延迟初始化 provider，在第一次使用时通过 context 获取）
        self._darwin: Optional[DarwinEvolution] = None
        self._darwin_last_trigger_time = 0.0

        # Phase 2: 模式发现系统（延迟初始化 provider + log_collector）
        self._log_collector: Optional[LogCollector] = None
        self._pattern_discovery: Optional[PatternDiscovery] = None
        self._rule_generator: Optional[RuleGenerator] = None

        # 轻量级指标收集器（在 main_event / main_command / main_evolution 中埋点）
        self.metrics = Metrics("yunli_plugin")

        # 创建三个协作者实例（必须在所有共享 state 初始化之后）
        self._event_pipeline = YunliEventPipeline(self)
        self._command_handler = YunliCommandHandler(self)
        self._evolution_manager = YunliEvolutionManager(self)

        # 记忆维护定时任务配置（惰性启动，避免测试中协程未 await 警告）
        self._memory_cleanup_interval = self.config.get("memory_cleanup_interval_seconds", 3600)
        self._memory_maintenance_started = False

    # ========== 后台任务管理 ==========

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
                self.logger.error("后台任务异常", exc_info=t.exception())

        task.add_done_callback(_done_callback)
        return task

    async def _periodic_memory_maintenance(self):
        """后台定时任务：定期清理过期记忆 + 衰减置信度 + 生成群聊摘要

        每小时执行一次：
        1. cleanup_expired：清理过期记忆、约定，衰减置信度
        2. generate_group_summaries：为活跃群生成群聊摘要

        P2-8 修复：添加重试上限和指数退避，避免连续失败导致日志爆炸
        """
        consecutive_failures = 0
        max_consecutive_failures = 5
        base_retry_delay = 60  # 基础重试延迟（秒）

        while True:
            try:
                await asyncio.sleep(self._memory_cleanup_interval)
                # 1. 清理过期记忆 + 置信度衰减
                await asyncio.to_thread(self._memory_manager.cleanup_expired)
                # 2. 生成群聊摘要
                await self._memory_manager.generate_group_summaries()
                # 成功则重置失败计数
                consecutive_failures = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_failures += 1
                # 指数退避：60s → 120s → 240s → 480s → 960s
                retry_delay = min(
                    base_retry_delay * (2 ** (consecutive_failures - 1)),
                    3600,  # 最大 1 小时
                )
                self.logger.error(
                    "记忆维护任务失败 (%d/%d): %s，%d 秒后重试",
                    consecutive_failures, max_consecutive_failures, e, retry_delay,
                )
                if consecutive_failures >= max_consecutive_failures:
                    self.logger.error(
                        "记忆维护任务连续失败 %d 次，暂停本轮重试，等待下一周期",
                        max_consecutive_failures,
                    )
                    consecutive_failures = 0
                    await asyncio.sleep(self._memory_cleanup_interval)
                else:
                    await asyncio.sleep(retry_delay)

    def _init_data(self) -> None:
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
                self.logger.info("初始数据导入完成")
            else:
                self.logger.warning("未找到初始数据文件 %s", data_path)

    # ========== @filter.on_llm_request / response 装饰器入口（委派给 EventPipeline） ==========

    @filter.on_message(priority=1)
    async def on_message(self, event: AstrMessageEvent):
        """所有消息常驻监听：确保非@云璃的群聊消息也能被记录

        P2-12 修复：原 on_llm_request 只在 AstrBot 调用 LLM 时触发，
        导致非@消息无法进入旁听模式。新增 on_message handler，
        每条消息都尝试记录，不管是否 @ 云璃。
        """
        # 惰性启动记忆维护定时任务（首次调用时启动）
        if not self._memory_maintenance_started:
            self._memory_maintenance_started = True
            self._safe_create_task(self._periodic_memory_maintenance())
        return await self._event_pipeline.on_message(event)

    @filter.on_llm_request(priority=50)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """LLM 请求前：注入云璃人格提示词（委派给 YunliEventPipeline）"""
        return await self._event_pipeline.on_request(event, req)

    @filter.on_llm_response(priority=50)
    async def on_llm_response(self, event: AstrMessageEvent, response: LLMResponse):
        """LLM 响应后：拟人化/分段/Token 监控（委派给 YunliEventPipeline）"""
        return await self._event_pipeline.on_response(event, response)

    # ========== 防抖器回调（保留为实例方法供 MessageDebouncer 调用） ==========

    async def _on_debounce_flush(
        self, scope: str, event: AstrMessageEvent, req: ProviderRequest
    ):
        """防抖窗口到期后，处理合并后的消息（委派给 EventPipeline）"""
        await self._event_pipeline._on_debounce_flush(scope, event, req)

    async def _on_individual_message(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """防抖合并时，对每条原始消息单独触发记忆提取

        避免"我喜欢猫"+"我喜欢狗"合并后只提取出一条偏好。
        仅执行轻量记忆提取，不注入人格提示词、不生成回复。
        """
        try:
            group_id = self._get_group_id(event)
            user_id = self._get_user_id(event)
            user_nickname = self._get_user_nickname(event)
            message = event.message_str or ""
            if not group_id or not user_id or not message:
                return
            if self._memory_manager._lightweight_enabled:
                self._memory_manager.extract_memory_lightweight(
                    group_id, user_id, message, user_nickname
                )
        except Exception:
            self.logger.debug("防抖单条消息记忆提取失败", exc_info=True)

    # ========== @filter.command 装饰器入口（委派给 CommandHandler / EvolutionManager） ==========

    @filter.command("云璃")
    async def cmd_yunli(self, event: AstrMessageEvent):
        """/云璃 命令入口（委派给 YunliCommandHandler）"""
        async for result in self._command_handler.cmd_yunli(event):
            yield result

    @filter.command("云璃语音")
    async def cmd_voice(self, event: AstrMessageEvent):
        """/云璃语音 命令入口（委派给 YunliCommandHandler）"""
        async for result in self._command_handler.cmd_voice(event):
            yield result

    @filter.command("云璃资料")
    async def cmd_knowledge(self, event: AstrMessageEvent):
        """/云璃资料 命令入口（委派给 YunliCommandHandler）"""
        async for result in self._command_handler.cmd_knowledge(event):
            yield result

    @filter.command("云璃帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        """/云璃帮助 命令入口（委派给 YunliCommandHandler）"""
        async for result in self._command_handler.cmd_help(event):
            yield result

    @filter.command("云璃进化")
    async def cmd_darwin(self, event: AstrMessageEvent):
        """/云璃进化 命令入口（委派给 YunliEvolutionManager）"""
        async for result in self._evolution_manager.cmd_darwin(event):
            yield result

    # ========== 共享辅助方法（测试直接访问） ==========

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
        except (AttributeError, TypeError, ValueError):
            self.logger.debug("获取 self_id 失败", exc_info=True)
        self._cached_self_id = ""
        return ""

    def _get_self_id(self) -> str:
        """获取机器人自身QQ号（委托缓存版本）"""
        return self._get_cached_self_id()

    def _get_group_id(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_group_id())
        except (AttributeError, TypeError, ValueError):
            self.logger.debug("获取 group_id 失败", exc_info=True)
            return ""

    def _get_user_id(self, event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id())
        except (AttributeError, TypeError, ValueError):
            self.logger.debug("获取 user_id 失败", exc_info=True)
            return ""

    def _get_user_nickname(self, event: AstrMessageEvent) -> str:
        try:
            return event.get_sender_name() or ""
        except (AttributeError, TypeError, ValueError):
            self.logger.debug("获取 sender_name 失败", exc_info=True)
            return ""

    def _is_segmented_reply_enabled(self) -> bool:
        """检查 AstrBot 是否启用了分段回复"""
        try:
            # 从 AstrBot 配置中读取分段回复设置
            if hasattr(self.context, 'config') and self.context.config:
                platform_settings = getattr(self.context.config, 'platform_settings', None)
                if platform_settings:
                    segmented = platform_settings.get('segmented_reply', {})
                    return segmented.get('enable', False)
        except (AttributeError, TypeError, KeyError):
            self.logger.debug("读取分段回复配置失败", exc_info=True)

        # 默认启用插件自身的分段逻辑（如果用户配置了的话）
        return self.config.get('force_segmented_reply', True)

    def _prepare_segments(self, text: str) -> List[Dict]:
        """将文本切分为段（短消息策略：最多2段，段间衔接自然化）

        不再做空段过滤和 Markdown 检测，交由简化后的 MessageSplitter 处理。
        第2段开头随机添加连接词，模拟真人补充说话的自然感。
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

            # 段间衔接自然化：第2段开头随机加连接词（30%概率）
            if i > 0 and random.random() < 0.3:
                connectors = [
                    "而且", "不过", "对了", "还有", "话说",
                    "…不过", "…而且", "…对了",
                ]
                connector = random.choice(connectors)
                # 避免重复：如果段首已有连接词则跳过
                if not any(seg_text.startswith(c) for c in connectors):
                    seg_text = connector + "，" + seg_text

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

    # ========== 交互日志（被 EventPipeline / Phase2 / 测试直接调用） ==========

    async def _log_interaction(self, group_id, user_id, user_nickname, message, response, trigger_type, response_filtered=""):
        """记录交互日志到记忆系统 + Phase2 日志采集

        异步方法：通过 asyncio.to_thread 将同步 DB 操作放到线程池，
        避免阻塞事件循环，同时确保 _safe_create_task 能正确包装为协程。
        """
        emotion_state = self.persona_engine.emotion.current_state if hasattr(self.persona_engine, 'emotion') else ""

        # 同步的 DB 操作放到线程池执行
        await asyncio.to_thread(
            self._memory_manager.log_interaction,
            group_id, user_id, user_nickname, message, response, trigger_type,
            emotion_state=emotion_state,
            on_atmosphere_update=self._group_perception.update_atmosphere,
            on_topic_update=self._group_perception.detect_topic,
        )

        # Phase 2: 对话日志采集（非阻塞，采样写入）
        self._evolution_manager._get_log_collector().collect(InteractionLog(
            group_id=group_id, user_id=user_id, user_nickname=user_nickname,
            message=message, response_raw=response, response_filtered=response_filtered or response,
            emotion_state=emotion_state, trigger_type=trigger_type,
        ))

        # 话题线程追踪（不阻塞主流程）
        if self._enable_topic_threads and group_id and user_id:
            self._group_perception.update_topic_threads(
                group_id, user_id, user_nickname, message,
                self._topic_thread_ttl_minutes, self._max_topic_threads,
            )

    # ========== 生命周期 ==========

    async def close(self):
        """显式清理资源：取消后台任务、刷新DB日志缓冲区、关闭连接

        替代 __del__ 的可靠清理方式，应在插件卸载时显式调用。
        """
        # 1. 取消所有未完成的后台任务
        for task in list(self._background_tasks):
            if not task.done():
                task.cancel()
        self._background_tasks.clear()

        # 2. 刷新DB日志缓冲区
        if hasattr(self, "db") and hasattr(self.db, "flush_logs"):
            try:
                self.db.flush_logs()
            except (OSError, RuntimeError):
                self.logger.warning("close(): 刷新 DB 日志缓冲区失败", exc_info=True)

        # 3. 清空防抖缓冲区
        if hasattr(self, "_debouncer"):
            self._debouncer.clear()

        # 4. 关闭数据库连接
        if hasattr(self, "db"):
            try:
                self.db.close()
            except (OSError, RuntimeError):
                self.logger.warning("close(): 关闭数据库连接失败", exc_info=True)

        # 5. 输出运行期指标摘要（便于管理员在卸载时快速查看运行状况）
        if hasattr(self, "metrics"):
            try:
                self.metrics.log_summary(self.logger)
            except (OSError, RuntimeError, ValueError):
                self.logger.debug("close(): 输出 metrics 摘要失败", exc_info=True)

    def _cleanup_sync(self):
        """同步清理核心逻辑（close 的非 async 子集，供 __del__ 复用）

        close() 是 async 方法，__del__ 中无法 await。
        此方法提取 close() 的同步清理步骤，避免维护两份重复代码。
        """
        # 1. 取消所有未完成的后台任务
        for task in list(getattr(self, "_background_tasks", set())):
            if not task.done():
                task.cancel()
        self._background_tasks.clear()

        # 2. 刷新DB日志缓冲区
        if hasattr(self, "db") and hasattr(self.db, "flush_logs"):
            try:
                self.db.flush_logs()
            except (OSError, RuntimeError):
                self.logger.debug("清理: 刷新 DB 日志缓冲区失败", exc_info=True)

        # 3. 清空防抖缓冲区
        if hasattr(self, "_debouncer"):
            try:
                self._debouncer.clear()
            except (OSError, RuntimeError):
                pass

        # 4. 关闭数据库连接
        if hasattr(self, "db"):
            try:
                self.db.close()
            except (OSError, RuntimeError):
                self.logger.debug("清理: 关闭数据库连接失败", exc_info=True)

    def __del__(self):
        """析构函数：尽力清理（不可靠，优先使用 close()）

        简化版：仅执行同步清理子集，不输出 metrics 摘要（避免 __del__ 中
        触发额外副作用）。所有异常静默处理，__del__ 绝不能抛异常。
        """
        try:
            self._cleanup_sync()
        except Exception:
            pass  # __del__ 中绝对不能抛异常
