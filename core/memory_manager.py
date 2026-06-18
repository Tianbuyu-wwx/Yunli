"""云璃插件 - 记忆管理模块

从 main.py 中提取的记忆管理逻辑：
- 轻量级规则式记忆提取（高频简单模式，零成本）
- LLM深度记忆整理（定时批量处理）
- 对话缓冲与记忆日志
"""

import asyncio
import json
import logging
import random
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# ========== 预编译正则（模块加载时编译一次，避免每次调用时重复编译） ==========

# 偏好提取模式
_PREFERENCE_PATTERNS = [
    (re.compile(r"我(?:喜欢|爱|讨厌)(.+?)[，。！]"), "preference", 6),
    (re.compile(r"我(?:超|最)(?:爱|喜欢)(.+?)[，。！]"), "preference", 7),
    (re.compile(r"我(?:不(?:喜欢|爱)|受不了|反感|嫌弃)(.+?)[，。！]"), "preference", 6),
    (re.compile(r"(.+?)是我(?:的)?最爱[，。！]?"), "preference", 7),
    (re.compile(r"我最(?:喜欢|爱|讨厌)(?:的是|就是)?(.+?)[，。！]"), "preference", 7),
    (re.compile(r"我对(.+?)(?:很|挺|非常|特别)(?:感兴趣|有兴趣|喜欢)[，。！]"), "preference", 6),
    (re.compile(r"我(?:沉迷|热衷|迷上|入坑)(?:了)?(.+?)[，。！]"), "preference", 6),
    # P2-6 修复：扩展偏好提取规则，覆盖更多口语化表达
    (re.compile(r"我(?:偏好|倾向|钟情|偏爱|偏好)(.+?)[，。！]"), "preference", 6),
    (re.compile(r"我(?:比较|更)(?:喜欢|爱|偏好)(.+?)[，。！]"), "preference", 6),
    (re.compile(r"我(?:一般|平时|通常)(?:都)?(?:喜欢|爱|吃|喝|玩|看|听)(.+?)[，。！]"), "preference", 5),
    (re.compile(r"我(?:特别|超|巨|贼|可)(?:喜欢|爱|想)(.+?)[，。！]"), "preference", 7),
    (re.compile(r"我(?:不太|不怎么|不大)(?:喜欢|爱|感兴趣)(.+?)[，。！]"), "preference", 6),
]

# 身份/职业提取模式
_FACT_PATTERNS = [
    (re.compile(r"我是(?:一个|一名|个|位)?(.+?)(?:的|了)?[，。！]"), "fact", 5),
    (re.compile(r"我在(.+?)(?:工作|上班|上学|读书|实习|兼职)[，。！]"), "fact", 5),
    (re.compile(r"我(?:学|读|上)(?:的是)?(.+?)(?:专业|系|班|学校|大学)[，。！]"), "fact", 5),
    (re.compile(r"我(?:来自|是)(.+?)(?:人|的)[，。！]"), "fact", 5),
    (re.compile(r"我(?:住在|在)(.+?)(?:附近|旁边|里面|这里)[，。！]"), "fact", 5),
    # P2-6 修复：扩展身份提取规则
    (re.compile(r"我(?:叫|姓)(.+?)[，。！]"), "fact", 6),
    (re.compile(r"我(?:今年|现在)(\d+岁|多大)[，。！]?"), "fact", 5),
    (re.compile(r"我(?:是男|是女|男生|女生|男孩子|女孩子)"), "fact", 7),
    (re.compile(r"我(?:毕业|读完)(?:了|于)?(.+?)[，。！]"), "fact", 5),
    (re.compile(r"我(?:做|干|搞)(?:的是)?(.+?)(?:的|工作|活)[，。！]"), "fact", 5),
]

# 事件/状态提取模式
_EVENT_PATTERNS = [
    (re.compile(r"我(?:今天|现在|正在)(.+?)[，。！]"), "event", 5, 1),
    (re.compile(r"我(?:昨天|前天)(.+?)[，。！]"), "event", 5, 3),
    (re.compile(r"我(?:最近|这几天|这两天)(.+?)[，。！]"), "event", 5, 7),
    (re.compile(r"我(?:周末|上周|上次|之前)(.+?)[，。！]"), "event", 5, 14),
    (re.compile(r"我(?:打算|准备|要|想)(?:去|做|玩|吃|看)(.+?)[，。！]"), "event", 5, 7),
]

# 拥有/能力提取模式
_ABILITY_PATTERNS = [
    (re.compile(r"我有(?:一个|一只|一把|一台|一辆|一张|一本)?(.+?)[，。！]"), "fact", 5),
    (re.compile(r"我会(.+?)[，。！]"), "fact", 5),
    (re.compile(r"我(?:能|可以)(.+?)[，。！]"), "fact", 5),
    (re.compile(r"我(?:擅长|精通|熟悉)(.+?)[，。！]"), "fact", 6),
]

# 约定完成信号模式
_COMPLETE_PATTERNS = [
    re.compile(r"搞定了"), re.compile(r"完成了"), re.compile(r"不用了"),
    re.compile(r"算了"), re.compile(r"做完了"),
    re.compile(r"已经(?:搞|做|完|搞)定?了?"),
]

# 未完成约定提取模式
_LOOP_PATTERNS = [
    re.compile(r"帮我(.+?)[，。！]"), re.compile(r"帮我(.+?)$"),
    re.compile(r"提醒我(.+?)[，。！]"), re.compile(r"提醒我(.+?)$"),
    re.compile(r"记得(.+?)[，。！]"), re.compile(r"记得(.+?)$"),
    re.compile(r"以后(?:要|会|去)(.+?)[，。！]"),
    re.compile(r"明天(?:要|会|去)(.+?)[，。！]"),
    re.compile(r"今晚(?:要|会|去)(.+?)[，。！]"),
    re.compile(r"下次(?:要|会|去)(.+?)[，。！]"),
]


class MemoryManager:
    """记忆管理器

    混合记忆架构：
    - 第一层：规则式轻量提取（实时，覆盖高频简单模式）
    - 第二层：LLM深度整理（定时，批量处理）
    """

    # 轻量提取否定词表：匹配到这些词时跳过提取，避免误提取口语化表达
    _EXTRACTION_STOP_WORDS = frozenset({
        # "我是"类误提取
        "是说", "是说啊", "是说呢", "是说吧", "是说嘛", "是说哦",
        "是说不是", "是说真的", "是说对吧",
        # "我会"类误提取
        "想你的", "想你的", "想办法", "想你了",
        "好好照顾", "好好珍惜",
        # "我有"类误提取
        "个问题", "个想法", "个建议", "个疑问", "个请求",
        "点事", "点想法", "点意见",
        # "记得"类误提取（约定误提取）
        "他说过", "她说过", "别人说", "有人说",
    })

    # 上下文否定/疑问词：提取内容前后出现这些词时跳过提取
    # 避免从"我不是学生""我是说真的""你觉得我是谁"中误提取
    _CONTEXT_NEGATION_WORDS = frozenset({
        "不是", "不", "没", "没有", "别", "莫", "勿", "非",
        "难道", "是否", "是不是", "会不会", "能不能",
        "觉得", "认为", "以为", "感觉", "好像", "似乎",
        "如果", "假如", "要是", "万一",
    })

    # 提取内容开头禁用词：以这些词开头的提取内容视为误提取
    # 如"说真的喜欢这个"以"说"开头，是口语化表达而非真实身份
    _CONTENT_PREFIX_BLACKLIST = frozenset({
        "说", "觉得", "认为", "感觉", "以为", "好像", "似乎",
        "真的", "确实", "其实", "反正", "毕竟",
        "不是", "没有", "别", "难道",
    })

    def __init__(self, db, config: dict = None, context=None):
        self.db = db
        self.config = config or {}
        self.context = context  # AstrBot Context（用于LLM调用）

        # 轻量提取配置
        self._lightweight_enabled = self.config.get("lightweight_extraction_enabled", True)

        # LLM深度整理配置
        self._memory_llm_enabled = self.config.get("memory_llm_enabled", True)
        self._llm_consolidation_hours = self.config.get("llm_consolidation_hours", 3)
        self._llm_consolidation_min_dialogues = self.config.get("llm_consolidation_min_dialogues", 15)

        # 对话积累缓冲（asyncio.Queue 自带线程安全，满时丢弃新消息）
        self._dialogue_queue: asyncio.Queue = asyncio.Queue(
            maxsize=self.config.get("memory_buffer_max_size", 2000)
        )
        self._last_deep_consolidation = time.time()

        # 后台缓冲任务防抖：避免每条消息都 create_task
        self._last_buffer_task_time = 0.0
        self._buffer_task_debounce = self.config.get("buffer_task_debounce_seconds", 60.0)

        # 单用户记忆上限
        self._max_memories_per_user = self.config.get("max_memories_per_user", 50)
        self._memory_consolidation_trigger = self.config.get(
            "memory_consolidation_trigger_threshold", 45
        )
        self._memory_consolidation_in_progress = set()

        # LLM整理并发控制：同时只允许一个整理任务运行
        self._consolidation_semaphore = asyncio.Semaphore(1)

        # P0-3 修复：LLM 调用超时保护（秒）
        # 避免因 LLM 提供方挂起导致信号量永久占用、整理任务永久阻塞
        self._llm_timeout_seconds = self.config.get("llm_timeout_seconds", 30.0)

        # 后台任务生命周期管理
        self._background_tasks: Set[asyncio.Task] = set()

    def _safe_create_task(self, coro) -> asyncio.Task:
        """创建后台任务并跟踪生命周期"""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)

        def _done_callback(t: asyncio.Task):
            self._background_tasks.discard(t)
            if not t.cancelled() and t.exception():
                logger.error("后台任务异常: %s", t.exception())

        task.add_done_callback(_done_callback)
        return task

    def log_interaction(
        self, group_id: str, user_id: str, user_nickname: str,
        message: str, response: str, trigger_type: str,
        emotion_state: str = "",  # 可选，兼容旧调用方
        on_atmosphere_update=None,  # 回调：更新群氛围
        on_topic_update=None,       # 回调：更新话题
    ):
        """记录互动日志（增强版，包含话题追踪和记忆整理）"""
        try:
            # 1. 记录基础互动
            self.db.log_interaction(
                group_id=group_id, user_id=user_id, user_nickname=user_nickname,
                message=message, response=response,
                trigger_type=trigger_type, emotion_state=emotion_state,
            )

            # 2. 更新话题和群氛围（通过回调避免循环依赖）
            if on_topic_update:
                topic = on_topic_update(message)
                if topic:
                    self.db.update_topic(group_id, topic, user_id)
            if on_atmosphere_update:
                on_atmosphere_update(group_id, message)

            # 3. 第一层：规则式轻量提取
            if self._lightweight_enabled:
                self.extract_memory_lightweight(group_id, user_id, message, user_nickname)

            # 4. 第二层：LLM深度整理（后台任务，带防抖）
            #    旁听模式（response 为空）不触发深度整理，避免空回复污染整理队列
            if self._memory_llm_enabled and response:
                now = time.time()
                if now - self._last_buffer_task_time >= self._buffer_task_debounce:
                    self._last_buffer_task_time = now
                    self._safe_create_task(
                        self.buffer_for_deep_consolidation(
                            group_id, user_id, user_nickname, message, response
                        )
                    )

        except Exception as e:
            logger.error("记录互动失败: %s", e)

    # ========== 消息缓冲与LLM深度整理 ==========

    async def buffer_for_deep_consolidation(
        self, group_id: str, user_id: str, user_nickname: str,
        message: str, response: str,
    ):
        """将对话缓冲到队列，等待定时LLM深度整理

        使用 asyncio.Queue(maxsize=2000) 自动背压：
        - 队列满 → 丢弃最不重要的新消息（drop-newest 策略）
        - 无需手动管理锁和超限丢弃
        """
        if not getattr(self, '_memory_llm_enabled', True):
            return

        # 入队（如果队列满则丢弃新消息，约 2000×200B ≈ 400KB 上限）
        try:
            self._dialogue_queue.put_nowait({
                "group_id": group_id, "user_id": user_id,
                "user_nickname": user_nickname,
                "message": message, "response": response,
                "timestamp": time.time(),
            })
        except asyncio.QueueFull:
            # 队列满 → 丢弃新消息，不阻塞当前流程
            return

        buffer_size = self._dialogue_queue.qsize()
        time_since_last = time.time() - self._last_deep_consolidation
        hours_since_last = time_since_last / 3600

        # 动态调整：根据消息密度自动调整门槛
        dynamic_hours = self._llm_consolidation_hours
        dynamic_min_dialogues = self._llm_consolidation_min_dialogues

        if buffer_size > 0 and self._is_config_reasonable():
            # 仅当有足够数据时计算密度（从队列快照采样）
            density = buffer_size / max(hours_since_last, 0.1)
            if density > 100:
                dynamic_hours = min(4, self._llm_consolidation_hours)
                dynamic_min_dialogues = min(80, self._llm_consolidation_min_dialogues)
            elif density > 30:
                dynamic_hours = min(2, self._llm_consolidation_hours)
                dynamic_min_dialogues = min(30, self._llm_consolidation_min_dialogues)
            else:
                dynamic_hours = min(3, self._llm_consolidation_hours)
                dynamic_min_dialogues = min(15, self._llm_consolidation_min_dialogues)

        dynamic_hours = min(dynamic_hours, self._llm_consolidation_hours)
        dynamic_min_dialogues = min(dynamic_min_dialogues, self._llm_consolidation_min_dialogues)

        should_consolidate = (
            hours_since_last >= dynamic_hours
            and buffer_size >= dynamic_min_dialogues
        )

        if should_consolidate:
            # 清空队列：将当前所有数据取出来处理
            conversations = []
            while not self._dialogue_queue.empty():
                try:
                    conversations.append(self._dialogue_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            self._last_deep_consolidation = time.time()
            if conversations:
                self._safe_create_task(self._deep_consolidate_memories(conversations))

    def _is_config_reasonable(self) -> bool:
        """检查配置值是否在合理范围（避免覆盖测试场景）"""
        return (
            self._llm_consolidation_hours <= 24
            and self._llm_consolidation_min_dialogues <= 100
        )

    async def _deep_consolidate_memories(self, conversations: List[Dict]):
        """LLM深度整理：批量合并、去重、升华记忆（受信号量控制并发）"""
        if not conversations:
            return

        async with self._consolidation_semaphore:
            try:
                provider = self.context.get_provider() if self.context else None
                if not provider:
                    return

                logger.info("开始LLM深度整理，共 %d 条对话", len(conversations))

                user_conversations = {}
                for conv in conversations:
                    key = (conv["group_id"], conv["user_id"])
                    if key not in user_conversations:
                        user_conversations[key] = {
                            "group_id": conv["group_id"], "user_id": conv["user_id"],
                            "user_nickname": conv["user_nickname"], "dialogs": [],
                        }
                    user_conversations[key]["dialogs"].append(conv)

                total_memories = 0
                for (group_id, user_id), user_data in user_conversations.items():
                    memories = await self._deep_extract_with_llm(
                        provider, group_id, user_id,
                        user_data["user_nickname"], user_data["dialogs"]
                    )
                    total_memories += len(memories)

                logger.info("LLM深度整理完成，共提取 %d 条记忆", total_memories)

            except Exception as e:
                logger.error("LLM深度整理失败: %s", e)

    async def _deep_extract_with_llm(
        self, provider, group_id: str, user_id: str,
        user_nickname: str, dialogs: List[Dict],
    ) -> List[Dict]:
        """调用LLM进行深度记忆提取"""
        dialog_lines = []
        total_chars = 0
        max_dialog_chars = 2000 if len(dialogs) > 50 else 3000
        for d in reversed(dialogs):
            # 标注用户昵称，避免群聊中多用户消息混淆
            nick = d.get("user_nickname", "") or d.get("user_id", "用户")[:4]
            line = f"{nick}: {d['message']}\n云璃: {d['response']}"
            if total_chars + len(line) > max_dialog_chars:
                break
            dialog_lines.append(line)
            total_chars += len(line)

        dialog_text = "\n\n".join(reversed(dialog_lines))

        existing_limit = 5 if len(dialogs) > 50 else 10
        existing_memories = self.db.get_important_memories(
            group_id, user_id, min_confidence=5, limit=existing_limit
        )
        existing_text = ""
        if existing_memories:
            existing_text = "\n".join([
                f"- [{m.get('memory_type', 'fact')}] {m.get('content', '')}"
                for m in existing_memories
            ])

        system_prompt = """你是云璃的长期记忆整理专家。你的任务是从大量对话中提炼出高质量的记忆。

整理原则：
1. 合并重复信息：如果用户多次提到同一件事，只保留一条
2. 升华具体细节：把零散信息总结成概括性记忆
3. 识别隐含信息：从对话中推断用户的性格、习惯、关系
4. 区分重要程度：核心身份>长期偏好>短期事件
5. 过滤闲聊内容：只保留关于用户本身的信息

输出格式（JSON数组）：
[
  {"type": "fact", "content": "用户是大学生", "confidence": 9},
  {"type": "preference", "content": "喜欢吃辣的食物", "confidence": 8},
  {"type": "event", "content": "上周一起去爬山", "confidence": 6}
]

注意：
- content 简洁明确（10-15字）
- confidence 1-10
- 如果与已有记忆重复或高度相似，不要输出
- 如果没有新信息，输出空数组 []"""

        user_prompt = f"""请整理关于"{user_nickname}"的记忆。

=== 已有记忆（参考，避免重复） ===
{existing_text or "（无）"}

=== 新对话记录 ===
{dialog_text}

请输出整理后的记忆JSON数组。只输出新发现或需要更新的记忆。"""

        try:
            # P0-3 修复：添加超时保护，避免 LLM 挂起导致信号量永久占用
            llm_response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=user_prompt, system_prompt=system_prompt,
                ),
                timeout=self._llm_timeout_seconds,
            )
            if not llm_response or not llm_response.completion_text:
                return []

            memories = self._parse_memory_json(llm_response.completion_text)
            written = []
            for mem in memories:
                mem_type = mem.get("type", "fact")
                content = mem.get("content", "").strip()
                confidence = min(max(mem.get("confidence", 5), 1), 10)

                max_len = self.config.get("memory_max_content_length", 50)
                min_len = self.config.get("memory_min_content_length", 2)
                if not content or len(content) < min_len or len(content) > max_len:
                    continue

                is_duplicate = False
                for existing in existing_memories:
                    existing_content = existing.get("content", "").strip()
                    if content == existing_content or content in existing_content or existing_content in content:
                        is_duplicate = True
                        break
                if is_duplicate:
                    continue

                self.db.add_memory(
                    group_id, user_id, mem_type, content,
                    confidence=confidence, user_nickname=user_nickname,
                )
                written.append(mem)

            return written

        except asyncio.TimeoutError:
            logger.warning(
                "LLM深度提取超时（%s秒），跳过本次整理: %s",
                self._llm_timeout_seconds, user_nickname,
            )
            return []
        except Exception as e:
            logger.error("LLM深度提取失败 (%s): %s", user_nickname, e)
            return []

    def _parse_memory_json(self, text: str) -> List[Dict]:
        """解析LLM返回的记忆JSON"""
        if not text or not text.strip():
            return []

        text = text.strip()
        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        try:
            result = json.loads(text)
            if isinstance(result, list):
                return result
            return []
        except json.JSONDecodeError:
            pass

        match = re.search(r'\[.*?\]', text, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group(0))
                if isinstance(result, list):
                    return result
            except json.JSONDecodeError:
                pass

        memories = []
        for line in text.split('\n'):
            line = line.strip()
            if not line or line in ('[', ']'):
                continue
            if line.endswith(','):
                line = line[:-1]
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    memories.append(obj)
            except json.JSONDecodeError:
                pass

        return memories

    # ========== 轻量级记忆提取（第一层） ==========

    def extract_memory_lightweight(self, group_id: str, user_id: str, message: str,
                                    user_nickname: str = ""):
        """轻量级记忆提取：零成本正则匹配高频简单模式

        优化：不再"命中一条就 return"，改为收集最多 3 条记忆，
        确保偏好/身份/事件/能力/约定 五类都能独立检测。

        P2-3 修复：添加 try/except 保护，DB 异常不再中断整条链路。
        """
        if not self._lightweight_enabled:
            return

        try:
            self._extract_memory_lightweight_impl(group_id, user_id, message, user_nickname)
        except Exception as e:
            logger.error("轻量记忆提取失败: %s", e, exc_info=True)

    def _extract_memory_lightweight_impl(self, group_id: str, user_id: str, message: str,
                                          user_nickname: str = ""):

        def _try_add_memory(mem_type, content, confidence=5, expires_at=None, match=None):
            """尝试添加记忆，成功返回 True

            Args:
                match: 正则匹配对象，用于上下文校验（检查匹配位置前后的否定/疑问词）
            """
            if not (2 <= len(content) <= 15):
                return False
            # 否定词过滤：跳过口语化误提取
            for stop_word in MemoryManager._EXTRACTION_STOP_WORDS:
                if stop_word in content:
                    return False
            # 提取内容开头禁用词校验：避免"说真的喜欢这个"等口语化误提取
            for prefix in MemoryManager._CONTENT_PREFIX_BLACKLIST:
                if content.startswith(prefix):
                    return False
            # 上下文否定/疑问词校验
            # P0-4 修复：原逻辑仅检查匹配位置前 3 字符，存在两个缺陷：
            #   1. 范围太窄，"真的吗？我是学生"中"真的"在位置 0-1，
            #      "我是"在位置 4-5，context_before="的吗？"不包含"真的"
            #   2. 未检查匹配内容内部，"我是不是学生"会匹配并提取"是不是学生"
            # 修复方案：
            #   - 扩大前缀检查范围到 8 字符（覆盖常见口语前缀）
            #   - 增加对匹配内容本身的否定词检查（如"是不是""会不会"）
            if match is not None:
                start = match.start()
                # 扩大前缀检查范围到 8 字符
                context_before = message[max(0, start - 8):start]
                # 匹配到的完整文本（含前缀"我是""我喜欢"等）
                matched_text = match.group(0)
                for neg_word in MemoryManager._CONTEXT_NEGATION_WORDS:
                    if neg_word in context_before or neg_word in matched_text:
                        return False
            result = self.db.add_memory(
                group_id, user_id, mem_type, content,
                confidence=confidence, expires_at=expires_at,
                max_memories_per_user=self._max_memories_per_user,
                user_nickname=user_nickname,
            )
            if result.get("needs_consolidation"):
                self._safe_create_task(
                    self._consolidate_memories_for_user(group_id, user_id)
                )
            return True

        def _check_temporary():
            temporal_words = ["现在", "目前", "暂时", "最近", "这几天", "这两天"]
            return any(w in message for w in temporal_words)

        def _get_temp_expires(days=7):
            return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

        # 最多提取 3 条记忆
        max_extractions = 3
        extracted = 0

        # ===== 1. 偏好表达 =====
        if extracted < max_extractions:
            for pattern, mem_type, confidence in _PREFERENCE_PATTERNS:
                match = pattern.search(message)
                if match:
                    content = match.group(1).strip()
                    if _try_add_memory(
                        mem_type, content, confidence,
                        _get_temp_expires() if _check_temporary() else None,
                        match=match,
                    ):
                        extracted += 1
                    break  # 每类最多一条

        # ===== 2. 身份/职业 =====
        if extracted < max_extractions:
            for pattern, mem_type, confidence in _FACT_PATTERNS:
                match = pattern.search(message)
                if match:
                    content = match.group(1).strip()
                    if _try_add_memory(mem_type, content, confidence, match=match):
                        extracted += 1
                    break

        # ===== 3. 事件/状态 =====
        if extracted < max_extractions:
            for pattern, mem_type, confidence, expire_days in _EVENT_PATTERNS:
                match = pattern.search(message)
                if match:
                    content = match.group(1).strip()
                    if _try_add_memory(
                        mem_type, content, confidence, _get_temp_expires(expire_days),
                        match=match,
                    ):
                        extracted += 1
                    break

        # ===== 4. 拥有/能力 =====
        if extracted < max_extractions:
            for pattern, mem_type, confidence in _ABILITY_PATTERNS:
                match = pattern.search(message)
                if match:
                    content = match.group(1).strip()
                    if _try_add_memory(mem_type, content, confidence, match=match):
                        extracted += 1
                    break

        # ===== 约定完成信号（优先检测：完成消息不再添加新约定） =====
        is_complete_message = False
        for pattern in _COMPLETE_PATTERNS:
            if pattern.search(message):
                self.db.complete_open_loop(group_id, user_id)
                is_complete_message = True
                break

        # ===== 5. 未完成约定（仅在非完成消息中提取） =====
        if not is_complete_message and extracted < max_extractions:
            for pattern in _LOOP_PATTERNS:
                match = pattern.search(message)
                if match:
                    content = match.group(1).strip()
                    if 2 <= len(content) <= 30:
                        self.db.add_open_loop(group_id, user_id, content)
                        extracted += 1
                    break

    async def _consolidate_memories_for_user(self, group_id: str, user_id: str):
        """用户记忆达到上限时，调用LLM整理"""
        consolidation_key = f"{group_id}:{user_id}"
        if consolidation_key in self._memory_consolidation_in_progress:
            return

        self._memory_consolidation_in_progress.add(consolidation_key)

        try:
            logger.info("触发记忆整理: %s", consolidation_key)
            all_memories = self.db.get_memories(
                group_id, user_id, limit=200, include_outdated=False
            )

            if len(all_memories) < self._memory_consolidation_trigger:
                return

            user_nickname = ""
            for mem in all_memories:
                nick = mem.get("user_nickname", "")
                if nick:
                    user_nickname = nick
                    break
            if not user_nickname:
                user_nickname = f"群友{user_id[:4]}"

            memory_lines = []
            for i, mem in enumerate(all_memories, 1):
                mem_type = mem.get("type", "fact")
                content = mem.get("content", "")
                confidence = mem.get("confidence", 5)
                access = mem.get("access_count", 0)
                memory_lines.append(
                    f"{i}. [{mem_type}] {content} (置信度:{confidence}, 访问:{access})"
                )

            memory_text = "\n".join(memory_lines)

            system_prompt = """你是云璃的记忆整理专家。你的任务是优化和压缩用户的长期记忆。

整理原则：
1. 合并重复
2. 升华细节
3. 删除低价值
4. 保留核心

输出格式（JSON数组）：
[
  {"type": "fact", "content": "用户是大学生", "confidence": 9},
  {"type": "preference", "content": "喜欢吃辣的食物", "confidence": 8}
]

注意：
- content 简洁明确（10-15字）
- confidence 1-10
- 输出数量应控制在输入数量的60%以下"""

            user_prompt = f"""请整理以下记忆，将 {len(all_memories)} 条压缩到 {int(len(all_memories) * 0.6)} 条以下。

=== 现有记忆 ===
{memory_text}

请输出整理后的记忆JSON数组。只保留最有价值、不重复的记忆。"""

            try:
                provider = self.context.get_provider() if self.context else None
                if not provider:
                    logger.warning("未获取到LLM Provider，跳过整理")
                    return

                # P0-3 修复：添加超时保护，避免 LLM 挂起导致整理任务永久阻塞
                try:
                    response = await asyncio.wait_for(
                        provider.text_chat(
                            prompt=user_prompt, system_prompt=system_prompt,
                        ),
                        timeout=self._llm_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "LLM整理超时（%s秒），跳过本次整理: %s",
                        self._llm_timeout_seconds, consolidation_key,
                    )
                    return

                llm_response = ""
                if hasattr(response, "completion_text"):
                    llm_response = response.completion_text
                elif isinstance(response, str):
                    llm_response = response
                elif hasattr(response, "chain"):
                    for comp in response.chain:
                        if hasattr(comp, "text"):
                            llm_response += comp.text

                consolidated = self._parse_memory_json(llm_response)
                if not consolidated:
                    logger.warning("整理未返回有效结果，跳过")
                    return

                try:
                    # 事务保护：使用 replace_user_memories 原子性替换
                    # 失败时旧记忆保持不变，避免整理失败导致数据丢失
                    # P0-1 修复：written_count == 0 时旧记忆已保留（ROLLBACK）
                    written_count = self.db.memory_db.replace_user_memories(
                        group_id, user_id, consolidated, user_nickname
                    )
                    if written_count == 0:
                        logger.warning(
                            "整理后 0 条有效记忆，旧记忆 %d 条已保留: %s",
                            len(all_memories), consolidation_key,
                        )
                    else:
                        logger.info(
                            "整理完成: %d 条 → %d 条 (%s)",
                            len(all_memories), written_count, consolidation_key,
                        )
                except Exception as e:
                    logger.error("整理事务失败（旧记忆已保留）: %s", e)

            except Exception as e:
                logger.error("LLM调用失败: %s", e)

        finally:
            self._memory_consolidation_in_progress.discard(consolidation_key)

    def cleanup_expired(self):
        """清理过期记忆和约定，并执行置信度衰减"""
        self.db.cleanup_expired_memories()
        self.db.cleanup_expired_loops()
        # P1-3 修复：清理过期的互动日志和群聊摘要，避免表无限膨胀
        try:
            self.db.memory_db.cleanup_old_logs(retention_days=7)
        except Exception as e:
            logger.error("清理互动日志失败: %s", e)
        try:
            self.db.memory_db.cleanup_old_summaries(keep_count=48)
        except Exception as e:
            logger.error("清理群聊摘要失败: %s", e)
        # 置信度衰减：打破"富者愈富"反馈循环，让新记忆有机会浮现
        # P1-1 修复：min_confidence 从 3 提升到 5，与检索阈值一致，避免"软性遗忘"
        try:
            self.db.memory_db.decay_memory_confidence(
                decay_factor=0.95, min_confidence=5
            )
        except Exception as e:
            logger.error("置信度衰减失败: %s", e)

    async def generate_group_summaries(self):
        """为所有活跃群生成群聊摘要

        定期调用（如每小时），从 interaction_logs 提取最近 1 小时的对话，
        调用 LLM 生成摘要，写入 chat_summaries 表。
        这样云璃在被@时能引用群聊摘要上下文，了解群聊整体走向。
        """
        if not self._memory_llm_enabled or not self.context:
            return

        try:
            active_groups = self.db.memory_db.get_active_groups()
            for group_id in active_groups:
                # 检查是否已有最近 1 小时内的摘要，避免重复生成
                latest = self.db.memory_db.get_latest_summary(group_id)
                if latest and latest.get("end_time"):
                    try:
                        end_time = datetime.strptime(
                            latest["end_time"], "%Y-%m-%d %H:%M:%S"
                        )
                        if (datetime.now() - end_time).total_seconds() < 3600:
                            continue  # 1 小时内已生成过摘要
                    except Exception:
                        pass

                await self._generate_summary_for_group(group_id)
        except Exception as e:
            logger.error("群聊摘要生成失败: %s", e)

    async def _generate_summary_for_group(self, group_id: str):
        """为单个群生成摘要"""
        try:
            interactions = self.db.memory_db.get_recent_interactions(
                group_id, hours=1, limit=50
            )
            if len(interactions) < 5:
                return  # 互动太少，不值得生成摘要

            # 构建对话文本
            dialogue_lines = []
            active_users = set()
            for log in interactions:
                nickname = log.get("user_nickname", "群友")
                message = log.get("message", "")
                if message:
                    dialogue_lines.append(f"{nickname}: {message}")
                    active_users.add(nickname)
            dialogue_text = "\n".join(dialogue_lines[-30:])  # 最多 30 条

            system_prompt = """你是群聊摘要助手。请根据以下群聊记录生成简洁摘要。

输出格式（JSON）：
{
  "summary": "群聊主要话题和氛围的简短描述（30字以内）",
  "key_topics": ["话题1", "话题2"]
}

注意：
- summary 简洁概括群聊走向
- key_topics 提取 1-3 个核心话题
- 忽略无意义的寒暄和表情"""

            user_prompt = f"群号 {group_id} 最近 1 小时的对话记录：\n\n{dialogue_text}\n\n请生成摘要。"

            provider = self.context.get_provider() if self.context else None
            if not provider:
                return

            # P0-3 修复：添加超时保护，避免 LLM 挂起导致摘要生成永久阻塞
            try:
                response = await asyncio.wait_for(
                    provider.text_chat(
                        prompt=user_prompt, system_prompt=system_prompt,
                    ),
                    timeout=self._llm_timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "群聊摘要生成超时（%s秒），跳过群 %s",
                    self._llm_timeout_seconds, group_id,
                )
                return

            llm_response = ""
            if hasattr(response, "completion_text"):
                llm_response = response.completion_text
            elif isinstance(response, str):
                llm_response = response

            # 解析 JSON 响应
            import json as _json
            try:
                # 尝试提取 JSON 块
                json_start = llm_response.find("{")
                json_end = llm_response.rfind("}") + 1
                if json_start >= 0 and json_end > json_start:
                    data = _json.loads(llm_response[json_start:json_end])
                    summary = data.get("summary", "")
                    key_topics = data.get("key_topics", [])
                    if summary:
                        self.db.memory_db.add_summary(
                            group_id=group_id,
                            summary=summary,
                            key_topics=key_topics,
                            active_users=list(active_users),
                            message_count=len(interactions),
                        )
                        logger.info("群 %s 摘要生成: %s", group_id, summary)
            except _json.JSONDecodeError:
                # P2-9 修复：JSON 解析失败时降级为纯文本摘要
                logger.warning("群聊摘要 JSON 解析失败，尝试降级处理: %s", llm_response[:100])
                # 降级：直接使用 LLM 响应的前 30 字作为摘要
                fallback_summary = llm_response.strip()[:30]
                if fallback_summary and len(fallback_summary) >= 5:
                    self.db.memory_db.add_summary(
                        group_id=group_id,
                        summary=fallback_summary,
                        key_topics=[],
                        active_users=list(active_users),
                        message_count=len(interactions),
                    )
                    logger.info("群 %s 降级摘要: %s", group_id, fallback_summary)

        except Exception as e:
            logger.error("群 %s 摘要生成失败: %s", group_id, e)