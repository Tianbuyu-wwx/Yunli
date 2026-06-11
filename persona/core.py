import random
import re
from typing import Dict, List, Optional

from .emotion import EmotionStateMachine
from .language import LanguageStyleProcessor, MODERN_TERMS
from . import filters
from ..core import utils


class YunliPersonaEngine:
    """云璃人格引擎 - 协调各子系统工作"""

    # 精简版系统提示词（约400 Token）
    BASE_SYSTEM_PROMPT = """你是云璃，仙舟「朱明」的猎剑士，怀炎将军的孙女。你现在在一个叫QQ群的虚拟聚集地，和一群来自各地的人聊天。

【核心性格】
- 直率：说话不绕弯子，看不惯就直说
- 剑痴：看到什么都想跟剑扯上关系
- 毒舌傲娇：嘴上嘲讽，心里关心
- 大胃王：聊到吃的就兴奋
- 现代适应：不排斥新事物，会用剑的视角理解现代概念

【语言风格】
- 短句为主，偶尔带"哼""嘛""哦"
- 被夸奖时傲娇："哼，才不是为了你呢"
- 聊到剑时兴奋：话语变多，带感叹号
- 毒舌时直接："你这水平，跟我爷爷养的猫差不多"
- 只用文字说话，不输出表情符号、动作描述或情感标签

【QQ群聊规则】
- 只回应@，不主动插嘴
- 不回避现代概念，用有趣的方式理解
- 会记住常聊天的群友，对不同人态度不同
- 消息不要太长，一句话能说完的事不要用两句话，尽量控制在1-2句话
- 绝对禁止输出动作描述、情感标签、表情符号。不要写*动作*、（动作）、【标签】、<动作>、emoji等任何非文字内容

【数据查询规则】
当你需要回答关于自己、剧情、世界观的问题时，会查询数据库获取准确信息。"""

    def __init__(self, db, config: dict = None):
        self.db = db
        self.config = config or {}
        self.emotion = EmotionStateMachine(config.get("emotion", {}))
        self.language = LanguageStyleProcessor(config.get("language", {}))

        # 缓存（使用LRU策略，限制最大条目数防止内存泄漏）
        self._max_cache_size = self.config.get("max_cache_size", 100)
        self._prompt_cache = {}
        # 使用 OrderedDict 实现真正的 LRU 缓存
        from collections import OrderedDict
        self._knowledge_cache = OrderedDict()

    def build_system_prompt(self) -> str:
        """构建精简系统提示词"""
        prompt_parts = [self.BASE_SYSTEM_PROMPT]

        # 添加配置中的自定义提示词
        custom_append = self.config.get("custom_prompt_append", "")
        if custom_append:
            prompt_parts.append(f"\n【自定义设定】\n{custom_append}")

        return "\n".join(prompt_parts)

    def build_dynamic_prompt(self, context_data: Dict, token_budget: int = 300) -> str:
        """根据上下文动态构建提示词（Token预算控制版）

        Args:
            context_data: 上下文数据
            token_budget: Token预算上限（默认300）
        """
        parts = []
        used_tokens = 0

        # 1. 相关知识（最高优先级，最多3条）
        if context_data.get("relevant_knowledge"):
            knowledge_lines = ["相关背景："]
            for k in context_data["relevant_knowledge"][:3]:
                line = f"- {k['entity_name']}: {k['description']}"
                # 截断过长的描述
                if len(line) > 60:
                    line = line[:60] + "…"
                knowledge_lines.append(line)
                used_tokens += len(line) // 2
                if used_tokens >= token_budget * 0.6:  # 知识最多占60%预算
                    break
            parts.append("\n".join(knowledge_lines))

        # 2. 现代概念类比（仅在非查询模式下，且概率触发，最多1条）
        remaining_budget = token_budget - used_tokens
        if (
            remaining_budget > 50
            and context_data.get("analogies")
            and not getattr(self, "_is_knowledge_query", False)
            and random.random() < 0.3
        ):  # 30%概率触发类比，避免生硬
            analogy = context_data["analogies"][0]
            # 用自然叙述方式，不用标签格式，避免LLM模仿输出标签
            line = f"你可以这样理解：{analogy['yunli_analogy']}"
            # 截断过长的类比
            if len(line) > 40:
                line = line[:40] + "…"
            parts.append(line)
            used_tokens += len(line) // 2

        # 3. 用户历史（极简）
        remaining_budget = token_budget - used_tokens
        if remaining_budget > 20 and context_data.get("user_history"):
            stats = context_data["user_history"]
            total = stats.get("total", 0)
            if total > 0:
                parts.append(f"你们聊过{total}次了")
                used_tokens += 10

        # 4. 情感状态（极简，只在非中性时添加）
        remaining_budget = token_budget - used_tokens
        if (
            remaining_budget > 15
            and hasattr(self, "emotion")
            and self.emotion.current_state != "neutral"
        ):
            parts.append(
                f"你现在{self.emotion.EMOTION_STATES[self.emotion.current_state]['description']}"
            )

        return "\n".join(parts) if parts else ""

    def _add_to_knowledge_cache(self, key: str, value):
        """添加知识到缓存（LRU策略，超限时清理最久未访问条目）"""
        # 如果 key 已存在，先删除旧位置（后续会移到末尾表示最新使用）
        if key in self._knowledge_cache:
            del self._knowledge_cache[key]
        # 超限时移除最久未访问的条目（OrderedDict 头部）
        while len(self._knowledge_cache) >= self._max_cache_size:
            self._knowledge_cache.popitem(last=False)
        self._knowledge_cache[key] = value

    def get_context_data(self, message: str, group_id: str, user_id: str) -> Dict:
        """获取动态上下文数据"""
        data = {"relevant_knowledge": [], "analogies": [], "user_history": None}

        # 检测是否为知识查询模式
        mode = self.language.detect_query_mode(message)
        self._is_knowledge_query = mode == "knowledge_query"

        # 提取关键词
        keywords = self.language.extract_keywords(message)

        # 知识查询模式下，扩展关键词提取
        if self._is_knowledge_query:
            # 添加更多可能的知识相关词
            knowledge_keywords = [
                "云璃",
                "老铁",
                "朱明",
                "魔剑",
                "仙舟",
                "怀炎",
                "岁阳",
                "镕兵",
            ]
            for kw in knowledge_keywords:
                if kw in message and kw not in keywords:
                    keywords.append(kw)

        # 查询相关知识（带LRU缓存）
        query_limit = 3 if self._is_knowledge_query else 2  # 查询模式多查几条
        for kw in keywords[:query_limit]:
            cache_key = f"knowledge_{kw}"
            if cache_key in self._knowledge_cache:
                # 命中缓存，移到末尾标记为最近使用
                knowledge = self._knowledge_cache.pop(cache_key)
                self._knowledge_cache[cache_key] = knowledge
            else:
                knowledge = self.db.query_knowledge(kw, limit=query_limit)
                self._add_to_knowledge_cache(cache_key, knowledge)

            data["relevant_knowledge"].extend(knowledge)

        # 查询现代概念类比（非查询模式，且仅当消息明确包含现代词汇时）
        # 限制：消息长度不超过30字才查询类比，避免联网搜索长文本导致大量类比
        if not self._is_knowledge_query and len(message) <= 30:
            # 只有在消息中明确提到现代概念时才查询类比
            found_modern = False
            for term in MODERN_TERMS:
                if term in message:
                    analogy = self.db.query_analogy(term)
                    if analogy:
                        data["analogies"].append(analogy)
                        found_modern = True
                    break

            # 如果没有找到精确匹配，尝试关键词匹配（但降低优先级）
            if not found_modern:
                for kw in keywords:
                    if any(mw in kw for mw in ["机", "网", "游", "电"]):
                        analogy = self.db.query_analogy(kw)
                        if analogy:
                            data["analogies"].append(analogy)
                        break

        # 查询用户历史
        if self.config.get("remember_users", True):
            data["user_history"] = self.db.get_user_stats(group_id, user_id)

        return data

    def polish_response(self, text: str, message: str = "", is_first_segment: bool = True, skip_emotion: bool = False) -> str:
        """润色响应，确保符合云璃风格

        三种模式处理：
        - knowledge_query: 知识查询 → 最小过滤，保留格式，不注入情感
        - chat: 普通聊天 → 完整过滤，注入情感，拟人化处理
        - mixed: 混合模式（角色介绍知识）→ 中等过滤，轻度情感

        Args:
            text: LLM 原始响应
            message: 用户消息（仅用于模式检测）
            is_first_segment: 是否为第一段（后续片段不加语气词）
            skip_emotion: 是否跳过情绪再检测（on_llm_response 路径应传入 True，
                          因为 Phase 1 已经完成了情绪转换）
        """
        # 检测当前模式（查询模式 vs 聊天模式）
        mode = self.language.detect_query_mode(message)

        # 仅当 Phase 1 未处理时检测情绪触发器
        # on_llm_response 路径跳过此步骤（Phase 1 已触发）
        if not skip_emotion and mode != "knowledge_query":
            trigger = self.emotion.detect_trigger(message)
            if trigger:
                self.emotion.transition(trigger, message)
            else:
                self.emotion.auto_decay()

        # 应用语言风格（传入模式和是否第一段）
        text = self.language.apply_style(text, self.emotion.current_state, mode=mode, is_first_segment=is_first_segment)

        # 情感注入（仅聊天模式）
        if mode != "knowledge_query":
            text = self.emotion.inject_emotion(text, self.db)

        # 身份保持过滤（根据模式选择过滤强度）
        if mode == "knowledge_query":
            # 知识查询模式：最小过滤，保留内容完整性
            text = self._maintain_identity_light(text)
        else:
            # 聊天模式：完整过滤
            text = self._maintain_identity(text)

        return text

    def _maintain_identity(self, text: str) -> str:
        """确保云璃身份保持，过滤动作描述和情感标签，但保留少量颜文字

        委托 filters.clean_text() 执行统一过滤，保留本方法特有的逻辑：
        - 内容为空的兜底处理
        - 第一人称一致性增强
        """
        if not self.config.get("strict_identity", True):
            return text

        text = filters.clean_text(text, mode="strict")

        # 内容为空兜底（clean_text 已返回"…"，但需确保配置检查正确）
        if not text.strip():
            return "…"

        # 确保第一人称一致性（只在明显缺少主语时添加）
        if "我" not in text and len(text) > 8 and not text.startswith(("哼", "哈", "哦", "嗯", "喂", "切", "你", "这", "那")):
            verb_starts = ["是", "有", "在", "去", "来", "做", "想", "要", "会", "能", "好", "不错", "还行", "应该", "可能"]
            if any(text.startswith(v) for v in verb_starts):
                text = "我觉得" + text

        return text

    def _maintain_identity_light(self, text: str) -> str:
        """轻量级身份保持（用于知识查询模式）

        委托 filters.clean_text() 执行统一过滤。
        """
        if not self.config.get("strict_identity", True):
            return text

        text = filters.clean_text(text, mode="light")

        # 如果过滤后为空，返回原内容（知识内容优先）
        if not text.strip():
            return text

        return text

    def get_emotion_state(self) -> str:
        """获取当前情感状态"""
        return self.emotion.current_state

    def get_direct_response(self, message: str) -> Optional[str]:
        """尝试从数据库获取直接响应（不走LLM）"""
        topic = self.language.detect_topic(message)

        # 问候语直接查数据库
        if topic == "greeting":
            dialogues = self.db.query_dialogues("greeting", limit=3)
            if dialogues:
                chosen = random.choice(dialogues)
                self.db.update_dialogue_usage(chosen["id"])
                return chosen["content"]

        # 告别语直接查数据库
        elif topic == "farewell":
            dialogues = self.db.query_dialogues("farewell", limit=3)
            if dialogues:
                chosen = random.choice(dialogues)
                self.db.update_dialogue_usage(chosen["id"])
                return chosen["content"]

        return None

    def get_voice_line(self, line_type: str = None) -> Optional[str]:
        """获取语音台词"""
        lines = self.db.query_voice_lines(line_type, limit=3)
        if lines:
            return random.choice(lines)["content"]
        return None

    def clear_cache(self):
        """清理缓存"""
        self._prompt_cache.clear()
        self._knowledge_cache.clear()

    def review_response(self, text: str, is_knowledge_query: bool = False, max_len: int = 200) -> str:
        """回复自审 - 检测并修复LLM回复中的问题

        检测项：
        1. 过长（非知识查询模式下超过 max_len 字）
        2. 助手腔开头（"好的，"/"当然，"/"以下是"等）
        3. 泄露内部状态（"记忆模块"/"提示词"/"系统指令"等）
        4. 重复标点过多

        Args:
            text: LLM回复文本
            is_knowledge_query: 是否为知识查询模式
            max_len: 非知识查询模式下的长度上限（主流程可传入关系限制的值，
                     避免两级截断浪费）

        Returns:
            修复后的文本
        """
        if not text or not text.strip():
            return text

        # 1. 早退截断：过长文本先截断，避免后续昂贵操作浪费在将被丢弃的内容上
        if not is_knowledge_query and len(text) > max_len:
            text = utils.truncate_at_sentence(text, max_len)

        # 2. 去除助手腔开头
        text = utils.remove_assistant_prefix(text)

        # 3. 移除泄露内部状态的行
        text = utils.remove_internal_state_lines(text)

        # 4. 清理重复标点
        text = utils.clean_repeated_punctuation(text)

        return text
